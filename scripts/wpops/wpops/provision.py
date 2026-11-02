"""Production stacks. Configuration is separate from starting containers or DNS."""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import state
from .config import INVENTORY_FILE, REPO_DIR, ConfigError, _HOST_RE, load_inventory, node_config
from .stacks import StackError, compose, stack_path, wp

PLUGIN_VERSION = '1.9.19'
PLUGIN_SHA256 = '34e99ee6032f5863b278aef690ebbfdb00290beb4eacbdae3ab931eb719b3a58'


def login_slug(value: str) -> str:
    value = value.strip('/')
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', value)
            or value in ('wp-admin', 'wp-json', 'wp-content', 'wp-includes', '404', 'feed', 'files', '0', 's', 'p', 'page', 'author', 'name', 'category', 'tag', 'rest-route')
            or 'wp-login' in value):
        raise ConfigError('login path must be one URL slug (a-z, 0-9, hyphens), excluding WordPress reserved paths')
    return value


def install_login_plugin(path: Path) -> None:
    """Keep a verified copy outside wp-content to reapply after restores."""
    target = path / 'managed-plugins'
    target.mkdir(exist_ok=True)
    with urllib.request.urlopen(f'https://downloads.wordpress.org/plugin/wps-hide-login.{PLUGIN_VERSION}.zip', timeout=60) as r:
        data = r.read(5 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != PLUGIN_SHA256:
        raise StackError('WPS Hide Login download checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.infolist():
            dest = (target / member.filename).resolve()
            if not dest.is_relative_to((target / 'wps-hide-login').resolve()):
                raise StackError('invalid plugin archive path')
        z.extractall(target)


def port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            return True
    return False


def create(name: str, mode: str, domain: str, port: int, login_path: str = '',
           subdomains: bool = True, noindex: bool = False) -> dict:
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', name):
        raise ConfigError('stack name must use a-z, 0-9, hyphens (max 31 characters)')
    if mode not in ('single', 'multisite') or not _HOST_RE.fullmatch(domain):
        raise ConfigError('choose single|multisite and a valid domain hostname')
    if not 1024 <= port <= 65535:
        raise ConfigError('port must be between 1024 and 65535')
    slug = login_slug(login_path) if login_path else ''
    with state.locked('provision'):
        inv, cfg = load_inventory(), node_config()
        origin = inv.origin(cfg['node'])
        path = REPO_DIR / 'stacks' / name
        if origin.kind != 'node':
            raise ConfigError('this origin must have kind=node')
        if name in origin.stacks or path.exists():
            raise ConfigError(f'stack {name} already exists')
        used_networks = set()
        for env in (REPO_DIR / 'stacks').glob('*/.env'):
            from .config import load_env
            existing = load_env(env)
            used_networks.add(existing.get("STACK_NET"))
            if existing.get("HTTP_PORT") == str(port):
                raise ConfigError(f'port {port} is already assigned to another stack')
        if port_in_use(port):
            # e.g. the legacy root stack's 127.0.0.1:8080, or another service.
            raise ConfigError(f'port {port} is already in use on 127.0.0.1')
        network = next((str(n) for n in range(1,255) if str(n) not in used_networks), None)
        if network is None:
            raise ConfigError("no free production network slots in 10.214.0.0/16")
        path.mkdir(parents=True, mode=0o700)
        env = {
            'COMPOSE_FILE': str(REPO_DIR / 'docker-compose.stack.yml'),
            'COMPOSE_PROJECT_NAME': f'wps-{name}', 'STACK_DIR': str(path),
            'WPOPS_REPO_DIR': str(REPO_DIR), 'STACK_ALIAS': f'wpops-{name}',
            'WPOPS_EDGE_NETWORK': 'wpops-edge', 'HTTP_PORT': str(port), 'STACK_NET': network,
            'WP_MODE': mode, 'WP_DOMAIN_CURRENT_SITE': domain,
            'WP_SUBDOMAIN_INSTALL': str(subdomains).lower(),
            'MYSQL_ROOT_PASSWORD': secrets.token_hex(24), 'MYSQL_PASSWORD': secrets.token_hex(24),
            'REDIS_PASSWORD': secrets.token_hex(24), 'WP_ADMIN_PASSWORD': secrets.token_urlsafe(24),
        }
        # Quoted .env values also work when the checkout path contains spaces.
        state.write_private_text(path / '.env', ''.join(f'{k}={json.dumps(v)}\n' for k, v in env.items()))
        (path / 'data').mkdir()
        (path / 'managed-plugins' / 'wps-hide-login').mkdir(parents=True)
        (path / 'policy').mkdir()
        policy = {'enabled': bool(slug), 'default': slug, 'blogs': {}}
        (path / 'policy' / 'login-paths.json').write_text(json.dumps(policy, indent=2)+'\n')
        write_login_locations(path, policy)
        headers = path / 'nginx' / 'http'
        headers.mkdir()
        (headers / 'headers.conf').write_text('add_header X-Robots-Tag "noindex, nofollow" always;\n' if noindex else '# Production indexing is enabled.\n')
        if slug:
            try:
                install_login_plugin(path)
            except Exception:
                shutil.rmtree(path)
                raise
        entry = {'path': str(path), 'mode': mode, 'service': f'http://wpops-{name}:80',
                 'ingress_network': 'wpops-edge', 'managed': True}
        inv.raw['origins'][origin.name].setdefault('stacks', {})[name] = entry
        tmp = INVENTORY_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(inv.raw, indent=2)+'\n')
        tmp.replace(INVENTORY_FILE)
    return {'stack': name, **entry, 'login_path': slug, 'next': f'wpops stack init {name}'}


def ensure_edge() -> str | None:
    """Create wpops-edge with its fixed subnet. Returns a warning when an older
    network with another subnet exists (Nginx then cannot tell connectors apart)."""
    from .stacks import EDGE_NETWORK, EDGE_SUBNET
    with state.locked('edge-network'):
        r = subprocess.run(['docker', 'network', 'inspect', EDGE_NETWORK, '--format',
                            '{{range .IPAM.Config}}{{.Subnet}} {{end}}'], capture_output=True, text=True)
        if r.returncode:
            subprocess.run(['docker', 'network', 'create', '--subnet', EDGE_SUBNET, EDGE_NETWORK],
                           check=True, capture_output=True)
            return None
        if EDGE_SUBNET not in (r.stdout or '').split():
            warning = (f'{EDGE_NETWORK} uses {(r.stdout or "").strip() or "an unknown subnet"}, not {EDGE_SUBNET}: '
                       'login rate limits cannot see visitor IPs behind the tunnel. '
                       'Recreate it (docs/deployments.md, "Visitor IPs behind the tunnel").')
            print('warning: ' + warning, file=sys.stderr)
            return warning
        return None



def initialise(name: str, email: str, user: str = 'admin', scheme: str = 'https') -> dict:
    from .config import load_env
    inv, cfg = load_inventory(), node_config()
    origin = inv.origin(cfg['node'])
    path = stack_path(origin, name)
    if not origin.stacks[name].get('managed'):
        raise StackError('init is only for stacks created by wpops stack create')
    env = load_env(path / '.env')
    ensure_edge()
    compose(origin, name, 'up', '-d', '--wait', timeout=600)
    install_runtime(origin, name)
    installed = wp(origin, name, 'core', 'is-installed', '--network' if env['WP_MODE']=='multisite' else '--quiet')
    if installed.returncode == 0:
        return {'stack': name, 'status': 'already-installed'}
    # Never replace a populated database or convert an existing single site implicitly.
    # `wp db tables` loads WordPress and fails on an empty database; ask MariaDB.
    tables = wp(origin, name, 'db', 'query', 'SHOW TABLES', '--skip-column-names')
    if tables.returncode or tables.stdout.strip():
        raise StackError('database is not empty or cannot be inspected; refusing automatic install')
    args = ['core', 'multisite-install' if env['WP_MODE']=='multisite' else 'install',
            f'--url={scheme}://{env["WP_DOMAIN_CURRENT_SITE"]}', f'--title={name}',
            f'--admin_user={user}', f'--admin_email={email}', '--skip-email', '--prompt=admin_password']
    if env['WP_MODE']=='multisite':
        args += ['--skip-config']
        if env['WP_SUBDOMAIN_INSTALL']=='true':
            args += ['--subdomains']
    # Password goes over stdin, never in process arguments or job logs.
    result = compose(origin, name, 'exec', '-T', 'wp-cron', 'wp', *args,
                     input_text=env['WP_ADMIN_PASSWORD']+'\n')
    rewrite = wp(origin, name, 'rewrite', 'structure', '/%postname%/')
    if rewrite.returncode:
        raise StackError(rewrite.stderr or rewrite.stdout)
    # WP-CLI echoes prompted values (the password) back; keep them out of the output.
    output = '\n'.join(line for line in result.stdout.splitlines()
                       if env['WP_ADMIN_PASSWORD'] not in line).strip()
    return {'stack': name, 'status': 'installed', 'output': output,
            'credentials': str(path / '.env')}


def set_login(name: str, slug: str, blog_id: int | None = None) -> dict:
    slug = login_slug(slug)
    inv, cfg = load_inventory(), node_config()
    origin = inv.origin(cfg['node'])
    if not origin.stacks.get(name, {}).get('managed'):
        raise StackError('login policy requires a managed stack')
    if blog_id is not None and (blog_id < 1 or origin.stacks[name]['mode'] != 'multisite'):
        raise ConfigError('--blog-id must be positive and requires a multisite stack')
    path = stack_path(origin, name)
    with state.locked(f'login-{name}'):
        install_login_plugin(path)
        if (path / 'data' / 'wordpress' / 'wp-content').is_dir():
            install_runtime(origin, name)
        file = path / 'policy' / 'login-paths.json'
        policy = json.loads(file.read_text())
        policy['enabled'] = True
        if blog_id is None:
            policy['default'] = slug
        else:
            # The plugin is network-wide: every blog needs an undisclosed default too.
            if not policy.get('default'):
                policy['default'] = 'login-' + secrets.token_hex(12)
            policy.setdefault('blogs', {})[str(blog_id)] = slug
        tmp = file.with_suffix('.json.tmp')
        with tmp.open('w') as f:
            json.dump(policy, f, indent=2)
            f.write('\n')
        tmp.replace(file)
        write_login_locations(path, policy)
    return {'stack': name, 'blog_id': blog_id, 'login_path': slug,
            'next': f'wpops stack compose {name} exec -T nginx nginx -s reload (if running)'}


def write_login_locations(path: Path, policy: dict) -> None:
    """Rate limit custom login paths just like /wp-login.php (also subdirectory blogs)."""
    nginx = path / 'nginx'
    nginx.mkdir(exist_ok=True)
    slugs = sorted({s for s in [policy.get('default', ''), *policy.get('blogs', {}).values()] if s})
    text = '# Managed custom login locations. Regenerate with wpops stack login.\n'
    if slugs:
        pattern = '|'.join(re.escape(login_slug(s)) for s in slugs)
        text += """location ~ ^/(?:[_0-9a-zA-Z-]+/)?(?:%s)/?$ {
    limit_req zone=wp_login burst=10 nodelay;
    include fastcgi_params;
    fastcgi_pass php-fpm;
    fastcgi_param SCRIPT_FILENAME $document_root/index.php;
    fastcgi_param SCRIPT_NAME /index.php;
    fastcgi_param HTTP_HOST $host;
    fastcgi_param SERVER_NAME $host;
    fastcgi_param HTTPS $fastcgi_https if_not_empty;
    fastcgi_read_timeout 300;
}
""" % pattern
    tmp = nginx / 'login.conf.tmp'
    tmp.write_text(text)
    tmp.replace(nginx / 'login.conf')


def install_runtime(origin, name: str) -> None:
    """Reapply deployment-owned files after startup or a wp-content restore.

    No files inside wp-content are read-only mounts, so SiteGuard can restore
    whole directories. The policy MU plugin itself lives outside wp-content.
    """
    from .stacks import install_host_alias, wp_content
    install_host_alias(origin, name, log=lambda _: None)
    path = stack_path(origin, name)
    source = path / 'managed-plugins' / 'wps-hide-login'
    if (source / 'wps-hide-login.php').exists():
        shutil.copytree(source, wp_content(origin, name) / 'plugins' / 'wps-hide-login', dirs_exist_ok=True)


def remove(name: str, detach_sites: bool = False, keep_files: bool = False) -> dict:
    """Unregister a stack: stop it (managed stacks: compose down, data stays on
    disk), drop it from the inventory and the registry, and move a managed
    stack's directory to stacks/_removed/<name>-<time>. Nothing is deleted.
    DNS records and tunnel ingress rules for its hostnames are not touched."""
    import time
    from .config import save_inventory
    from . import siteguard
    with state.locked('provision'):
        inv, cfg = load_inventory(), node_config()
        node = cfg['node']
        origin = inv.origin(node)
        if name not in origin.stacks:
            raise ConfigError(f'unknown stack {name}')
        st = origin.stacks[name]
        ovs = state.overrides()
        busy = sorted(d for d, o in ovs.items() if o.get('status') == 'active' and o.get('stack') == name)
        if busy:
            raise ConfigError(f'stack {name} serves active failover overrides ({", ".join(busy)}); roll them back first')
        mapped = [s.slug for s in inv.sites if (s.host_on(node) or {}).get('stack') == name]
        if mapped and not detach_sites:
            raise ConfigError(f'sites mapped to stack {name}: {", ".join(mapped)} '
                              '(remove them with wpops site remove, or pass --detach-sites)')
        refs = [n for n, src in inv.backup_sources.items() if src.origin == node and src.stack == name]
        if refs:
            raise ConfigError(f'backup sources use stack {name}: {", ".join(refs)}; edit backup_sources first')
        raw = json.loads(json.dumps(inv.raw))
        for s in raw.get('sites', []):
            if (s.get('hosts') or {}).get(node, {}).get('stack') == name:
                s['hosts'].pop(node)
        raw['sites'] = [s for s in raw.get('sites', []) if s.get('hosts')]
        raw['origins'][node]['stacks'].pop(name)
        path = Path(st['path'])
        stopped = None
        if st.get('managed') and path.exists():
            r = compose(origin, name, 'down', check=False, timeout=600)
            stopped = r.returncode == 0
            if not stopped:
                raise StackError(f'compose down failed for {name}: {(r.stderr or r.stdout or "").strip()[-300:]}')
        save_inventory(raw, INVENTORY_FILE)
        archived = None
        if st.get('managed') and path.exists() and not keep_files and path.parent == REPO_DIR / 'stacks':
            dest = REPO_DIR / 'stacks' / '_removed' / f'{name}-{time.strftime("%Y%m%d-%H%M%S")}'
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dest)
            archived = str(dest)
        siteguard.save_key(name, None)
        state.audit('local', 'stack.remove', {'stack': name, 'detached': mapped, 'archived': archived})
    from . import registry
    r = registry.refresh()
    return {'stack': name, 'stopped': stopped, 'detached_sites': mapped if detach_sites else [],
            'archived_to': archived, 'revision': r['revision'],
            'note': 'DNS records and tunnel ingress for its hostnames are unchanged'}
