"""Site registry, per-stack SiteGuard keys and the registry API. Offline: each
stack's Nginx is a local HTTP stand-in, WP-CLI calls are mocked."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/wpops'))
from wpops import agent, config, notify, ops, provision, registry, siteguard, state  # noqa: E402


class FakeStack:
    """Stand-in for one stack's Nginx on 127.0.0.1: pages and SiteGuard's health API."""

    def __init__(self):
        self.keys = set()
        self.sg_sites = None            # list of SiteGuard site rows, or None = no SiteGuard
        self.checked_at = int(time.time())
        self.down_hosts = set()
        self.requests = []
        stack = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body, ctype='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _any(self, method):
                host = self.headers.get('Host')
                stack.requests.append((method, host, self.path, self.headers.get('X-Forwarded-Proto')))
                if self.path.startswith('/wp-json/sgbackup/v1/health'):
                    if stack.sg_sites is None:
                        return self._reply(404, {'code': 'rest_no_route'})
                    auth = self.headers.get('Authorization', '')
                    if auth[7:] not in stack.keys:
                        return self._reply(403, {'code': 'forbidden'})
                    if method == 'POST':
                        stack.checked_at = int(time.time())
                    return self._reply(200, {'status': 'operational', 'checked_at': stack.checked_at, 'version': '1.6.4',
                                             'api_version': 2, 'sites': stack.sg_sites,
                                             'environment': {'wp_version': '6.9', 'php_version': '8.3.1',
                                                             'last_backup': 1700000000, 'backup_count': 3}})
                if host in stack.down_hosts:
                    return self._reply(502, b'bad gateway', 'text/html')
                return self._reply(200, b'<html>ok</html>', 'text/html')

            def do_GET(self):
                self._any('GET')

            def do_POST(self):
                self._any('POST')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def sg_row(site_id, url, status='operational', **kw):
    host = url.split('/')[2]
    return {'site_id': site_id, 'url': url, 'domain': host, 'status': status, 'http_code': 200, 'response_time': 0.1,
            'ssl_expiry_days': 60, 'ssl_expires_at': 0, 'ssl_issuer': 'CA', 'ssl_ok': True, 'ssl_error': '',
            'ssl_stale': False, 'ssl_last_success': 1, 'ssl_checked_at': int(time.time()), 'last_check': int(time.time()),
            'error': '', 'check_method': 'localhost', 'consecutive_failures': 0, **kw}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        self.inv_file = self.root / 'config/inventory.json'
        self.raw = {'origins': {'primary': {'kind': 'external', 'targets': [{'type': 'A', 'content': '192.0.2.1'}]},
                                'node-a': {'kind': 'node', 'stacks': {}, 'tunnels': {'customer': 'tid-1'}}},
                    'sites': []}
        self.save()
        sd = self.root / 'state'
        sd.mkdir()
        self.p = contextlib.ExitStack()
        self.addCleanup(self.p.close)
        for obj, key, val in [
                (state, 'STATE_DIR', sd), (state, 'LOCK_DIR', sd / 'locks'), (state, 'OVERRIDES_FILE', sd / 'overrides.json'),
                (state, 'AUDIT_LOG', sd / 'audit.log'), (notify, 'EVENTS_FILE', sd / 'events.jsonl'),
                (config, 'NODE_CONFIG_FILE', self.root / 'config/wpops.json'), (config, 'INVENTORY_FILE', self.inv_file),
                (provision, 'REPO_DIR', self.root), (provision, 'INVENTORY_FILE', self.inv_file),
                (agent, 'TOKEN_FILE', sd / 'agent-token.sha256')]:
            self.p.enter_context(patch.object(obj, key, val))
        loader = lambda path=None: config.load_inventory(path or self.inv_file)  # noqa: E731
        for mod in (registry, provision, agent, siteguard):
            if hasattr(mod, 'load_inventory'):
                self.p.enter_context(patch.object(mod, 'load_inventory', loader))
        import wpops.__main__ as cli
        self.p.enter_context(patch.object(cli, 'load_inventory', loader))
        self.p.enter_context(patch.object(notify, 'post_webhook', return_value={'posted': False}))
        (self.root / 'config/wpops.json').write_text(json.dumps({'node': 'node-a', 'registry': {'health_timeout': 5}}))
        self.stacks = {}

    def save(self):
        self.inv_file.write_text(json.dumps(self.raw))

    def add_stack(self, name, mode='single', domain=None):
        self.raw = json.loads(self.inv_file.read_text())
        fake = FakeStack()
        self.addCleanup(fake.close)
        path = self.root / 'stacks' / name
        (path / 'data/wordpress/wp-content/uploads').mkdir(parents=True)
        (path / '.env').write_text(f'HTTP_PORT="{fake.port}"\nWP_DOMAIN_CURRENT_SITE="{domain or name + ".example.net"}"\n')
        self.raw['origins']['node-a']['stacks'][name] = {'path': str(path), 'mode': mode, 'managed': True,
                                                        'service': f'http://wpops-{name}:80', 'ingress_network': 'wpops-edge'}
        self.save()
        self.stacks[name] = fake
        return fake

    def map_site(self, slug, stack, host, blog_id=None, domains=None):
        self.raw = json.loads(self.inv_file.read_text())
        h = {'stack': stack, 'host': host, **({'blog_id': blog_id} if blog_id else {})}
        self.raw['sites'].append({'slug': slug, 'primary': 'primary', 'cf_account': 'customer',
                                  'domains': domains or [f'{slug}.example.com'], 'hosts': {'node-a': h, 'primary': {'host': (domains or [f'{slug}.example.com'])[0]}}})
        self.save()

    def set_facts(self, stack, **facts):
        f = state.read_json(state.STATE_DIR / 'registry-facts.json', {}) if (state.STATE_DIR / 'registry-facts.json').exists() else {}
        f[stack] = {'checked_at': int(time.time()), 'ok_at': int(time.time()), 'wp': '6.9', 'php': '8.3.1',
                    'siteguard': None, 'plugins': [], 'blogs': [], **facts}
        state.write_json(state.STATE_DIR / 'registry-facts.json', f)

    def events(self):
        return [e['kind'] for e in notify.recent_events(0, 1000, after=0)]

    # ---------------------------------------------------------------- membership

    def test_every_stack_and_blog_is_a_site_with_stable_ids_and_a_change_feed(self):
        self.add_stack('shop')
        self.add_stack('net', 'multisite', 'net.example.net')
        self.map_site('shop', 'shop', 'shop.example.net')
        self.map_site('news', 'net', 'news.net.example.net', blog_id=2, domains=['news.example.com'])
        self.set_facts('net', blogs=[
            {'blog_id': 1, 'domain': 'net.example.net', 'path': '/', 'url': 'https://net.example.net/', 'name': 'Main'},
            {'blog_id': 2, 'domain': 'news.net.example.net', 'path': '/', 'url': 'https://news.net.example.net/', 'name': 'News'},
            {'blog_id': 3, 'domain': 'arts.net.example.net', 'path': '/', 'url': 'https://arts.net.example.net/', 'name': 'Arts'},
            {'blog_id': 4, 'domain': 'old.net.example.net', 'path': '/', 'url': 'https://old.net.example.net/', 'archived': 1}])
        r = registry.refresh()
        reg = state.read_json(state.STATE_DIR / 'registry.json', {})
        self.assertEqual(sorted(reg['sites']), ['node-a:net:1', 'node-a:net:2', 'node-a:net:3', 'node-a:shop:1'])
        self.assertEqual(r['revision'], 4)
        ids = {u: m['site_id'] for u, m in reg['sites'].items()}
        self.assertEqual(len(set(ids.values())), 4)
        news = reg['sites']['node-a:net:2']
        self.assertEqual((news['slug'], news['domains'], news['mapped']), ('news', ['news.example.com'], True))
        arts = reg['sites']['node-a:net:3']
        self.assertEqual((arts['slug'], arts['domains'], arts['name'], arts['mapped']), (None, ['arts.net.example.net'], 'Arts', False))
        # Nothing changed: no new revision, no events.
        self.assertEqual(registry.refresh()['changes'], 0)
        # A blog goes away, a stack is added: removed + added, ids never reused.
        self.set_facts('net', blogs=[{'blog_id': 1, 'domain': 'net.example.net', 'path': '/', 'url': 'https://net.example.net/'},
                                     {'blog_id': 2, 'domain': 'news.net.example.net', 'path': '/', 'url': 'https://news.net.example.net/'}])
        self.add_stack('blog')
        registry.refresh()
        reg2 = state.read_json(state.STATE_DIR / 'registry.json', {})
        self.assertNotIn('node-a:net:3', reg2['sites'])
        self.assertEqual(reg2['sites']['node-a:shop:1']['site_id'], ids['node-a:shop:1'])
        self.assertNotIn(reg2['sites']['node-a:blog:1']['site_id'], ids.values())
        feed = registry.changes(since=4)
        ops_ = sorted((c['op'], c['uid']) for c in feed['changes'])
        self.assertIn(('removed', 'node-a:net:3'), ops_)
        self.assertIn(('added', 'node-a:blog:1'), ops_)
        self.assertFalse(feed['reset'])
        kinds = self.events()
        self.assertEqual(kinds.count('registry.site_added'), 5)
        self.assertEqual(kinds.count('registry.site_removed'), 1)
        seqs = [e['seq'] for e in notify.recent_events(0, 1000, after=0)]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(len(notify.recent_events(0, 1000, after=seqs[-2])), 1)

    def test_change_feed_reports_reset_when_it_no_longer_reaches_back(self):
        self.add_stack('shop')
        registry.refresh()
        path = state.STATE_DIR / 'registry-changes.jsonl'
        path.write_text('')
        self.add_stack('blog')
        registry.refresh()
        self.assertTrue(registry.changes(since=0)['reset'])
        self.assertFalse(registry.changes(since=1)['reset'])

    # ------------------------------------------------------------------- health

    def test_health_uses_siteguard_with_the_node_key_and_direct_checks_otherwise(self):
        net = self.add_stack('net', 'multisite', 'net.example.net')
        shop = self.add_stack('shop')
        self.set_facts('net', siteguard='1.6.4', blogs=[
            {'blog_id': 1, 'domain': 'net.example.net', 'path': '/', 'url': 'https://net.example.net/'},
            {'blog_id': 2, 'domain': 'net.example.net', 'path': '/two/', 'url': 'https://net.example.net/two/'}])
        net.sg_sites = [sg_row(1, 'https://net.example.net/'), sg_row(2, 'https://net.example.net/two/', 'down', error='HTTP 500')]
        net.keys.add('k' * 48)
        siteguard.save_key('net', {'key': 'k' * 48, 'fp': siteguard.fingerprint('k' * 48), 'kind': 'node', 'enrolled_at': 1})
        shop.down_hosts.add('shop.example.net')
        registry.refresh_health()
        snap = registry.snapshot()
        by = {s['uid']: s for s in snap['sites']}
        self.assertEqual(by['node-a:net:1']['health']['source'], 'siteguard')
        self.assertEqual(by['node-a:net:2']['health']['status'], 'down')
        self.assertEqual(by['node-a:shop:1']['health'], {**by['node-a:shop:1']['health'], 'source': 'direct', 'status': 'down',
                                                        'http_code': 502, 'consecutive_failures': 1})
        net_stack = next(s for s in snap['stacks'] if s['name'] == 'net')
        self.assertEqual(net_stack['versions']['php'], '8.3.1')
        self.assertEqual(net_stack['backup']['siteguard_last'], 1700000000)
        # The agent always presents itself as https to the stack, with the site's own Host.
        self.assertTrue(all(r[3] == 'https' for r in net.requests + shop.requests))
        self.assertIn(('GET', 'shop.example.net', '/'), [r[:3] for r in shop.requests])
        # A rejected key (e.g. a restore replaced SiteGuard's options) is reported and checks fall back to direct.
        net.keys.clear()
        registry.refresh_health()
        h = state.read_json(state.STATE_DIR / 'registry-health.json', {})
        self.assertEqual(h['_stacks']['net']['sg_kind'], 'auth')
        self.assertEqual(h['node-a:net:2']['source'], 'direct')
        self.assertEqual(registry.enrolment_due(), ['net'])

    def test_rejected_key_is_reenrolled_but_not_more_than_every_30_minutes(self):
        net = self.add_stack('net', 'multisite')
        self.set_facts('net', siteguard='1.6.4')
        siteguard.save_key('net', {'key': 'old' * 16, 'fp': 'x', 'kind': 'node', 'enrolled_at': 1})
        net.sg_sites = []
        registry.refresh_health()
        self.assertEqual(registry.enrolment_due(), ['net'])
        calls = []

        def fake_enroll(origin, name, ver, host, replace=False, log=None):
            calls.append((name, ver, host))
            return {'enrolled': True, 'kind': 'node', 'fp': 'sgk-x', 'verified': True}
        with patch.object(registry, 'collect_facts', return_value={'wp': '6.9', 'siteguard': '1.6.4', 'home': 'https://net.example.net/', 'blogs': []}), \
                patch.object(registry.sg, 'enroll', side_effect=fake_enroll):
            registry.refresh_facts(['net'])
            self.assertEqual(calls, [('net', '1.6.4', 'net.example.net')])
            self.assertEqual(registry.enrolment_due(), [])
        self.assertIn('siteguard.enrolled', self.events())

    def test_stale_siteguard_data_triggers_one_forced_check(self):
        net = self.add_stack('net', 'multisite')
        net.sg_sites = [sg_row(1, 'https://net.example.net/')]
        net.keys.add('k' * 48)
        net.checked_at = int(time.time()) - 7200
        siteguard.save_key('net', {'key': 'k' * 48, 'kind': 'node', 'enrolled_at': 1})
        registry.refresh_health()
        posts = [r for r in net.requests if r[0] == 'POST']
        self.assertEqual(len(posts), 1)
        h = state.read_json(state.STATE_DIR / 'registry-health.json', {})
        self.assertEqual(h['node-a:net:1']['source'], 'siteguard')
        registry.refresh_health()
        self.assertEqual(len([r for r in net.requests if r[0] == 'POST']), 1)

    def test_siteguard_format_payload_has_unique_ids_summary_and_alerts(self):
        a = self.add_stack('a')
        self.add_stack('b')
        a.down_hosts.add('a.example.net')
        registry.refresh_health()
        p = registry.siteguard_payload('https://ops.example.net')
        self.assertEqual(p['api_version'], 2)
        self.assertEqual(p['summary']['total'], 2)
        self.assertEqual(p['summary']['down'], 1)
        self.assertEqual(p['status'], 'degraded')
        self.assertEqual(len({s['site_id'] for s in p['sites']}), 2)
        self.assertEqual([x['type'] for x in p['alerts']], ['site_down'])
        self.assertTrue(all(s['uid'].startswith('node-a:') and s['check_source'] == 'direct' for s in p['sites']))

    # --------------------------------------------------------------- enrolment

    def test_enrolment_uses_a_node_key_and_never_replaces_the_sites_own_key(self):
        net = self.add_stack('net', 'multisite')
        origin = config.load_inventory(self.inv_file).origin('node-a')
        seen = []

        def fake_compose(origin, name, *args, **kw):
            seen.append((args, kw.get('input_text')))
            if '--set-node' in args or '--set' in args:
                net.keys.add(kw['input_text'])
                net.sg_sites = []
            return subprocess.CompletedProcess(args, 0, 'Success', '')

        def fake_wp(origin, name, *args, **kw):
            if args[:2] == ('siteguardbackup', 'health_api_key'):
                return subprocess.CompletedProcess(args, 0, json.dumps({'enabled': False, 'key': {'has_key': True}}) + '\n', '')
            seen.append((args, None))
            return subprocess.CompletedProcess(args, 0, 'Success', '')
        with patch('wpops.stacks.compose', side_effect=fake_compose), patch('wpops.stacks.wp', side_effect=fake_wp):
            r = siteguard.enroll(origin, 'net', '1.6.4', 'net.example.net')
            self.assertTrue(r['enrolled'] and r['kind'] == 'node' and r['verified'], r)
            self.assertIn('--set-node', seen[0][0])
            self.assertTrue(any(a[:3] == ('siteguardbackup', 'health_config', 'enable') for a, _ in seen))
            key = siteguard.load_keys()['net']['key']
            self.assertNotIn(key, json.dumps([a for a, _ in seen]))          # stdin only, never argv
            self.assertEqual((state.STATE_DIR / 'siteguard-keys.json').stat().st_mode & 0o777, 0o600)
            r = siteguard.enroll(origin, 'net', '1.6.3', 'net.example.net')
            self.assertFalse(r['enrolled'])
            self.assertIn('1.6.4', r['reason'])
            seen.clear()
            r = siteguard.enroll(origin, 'net', '1.6.3', 'net.example.net', replace=True)
            self.assertTrue(r['enrolled'] and r['kind'] == 'site')
            self.assertIn('--set', seen[0][0])

    # ------------------------------------------------------------ inventory ops

    def test_site_add_uses_the_default_staging_name(self):
        raw = json.loads(self.inv_file.read_text())
        raw['origins']['node-a'].update(standby_domain='standby.example.net', staging_suffix='na1')
        self.inv_file.write_text(json.dumps(raw))
        self.add_stack('shop', domain='shop.example.com')
        self.add_stack('net', 'multisite')
        self.set_facts('net', blogs=[{'blog_id': 2, 'domain': 'newsexamplecom-na1.standby.example.net', 'path': '/',
                                      'url': 'https://newsexamplecom-na1.standby.example.net/', 'name': 'News'}])
        registry.refresh()
        unmapped = [m for m in registry.changes(0)['changes'] if m['uid'] == 'node-a:shop:1'][-1]['site']
        self.assertEqual(unmapped['staging_host'], 'shopexamplecom-na1.standby.example.net')
        r = registry.site_add('shop', 'shop', domains=['www.shop.example.com', 'shop.example.com'], cf_account='customer')
        self.assertEqual(r['host']['host'], 'shopexamplecom-na1.standby.example.net')
        # no host stored: the rule applies; the stack keeps its production siteurl
        entry = json.loads(self.inv_file.read_text())['sites'][0]['hosts']['node-a']
        self.assertEqual(entry, {'stack': 'shop', 'site_host': 'shop.example.com'})
        inv = config.load_inventory(self.inv_file)
        self.assertEqual(inv.site('shop').host_on('node-a')['host'], 'shopexamplecom-na1.standby.example.net')
        self.assertEqual(inv.site('shop').site_host_on('node-a'), 'shop.example.com')
        # a blog already installed under its default name needs neither host nor site_host
        registry.site_add('news', 'net', blog_id=2, domains=['news.example.com'], cf_account='customer')
        entry = json.loads(self.inv_file.read_text())['sites'][1]['hosts']['node-a']
        self.assertEqual(entry, {'stack': 'net', 'blog_id': 2})
        # an explicit host is an override and is kept
        registry.site_add('news', 'net', blog_id=2, host='news-test.standby.example.net')
        inv = config.load_inventory(self.inv_file)
        self.assertEqual(inv.site('news').host_on('node-a')['host'], 'news-test.standby.example.net')
        self.assertNotIn('host_default', inv.site('news').host_on('node-a'))

    def test_site_add_and_remove_update_inventory_and_registry(self):
        self.add_stack('shop')
        self.add_stack('net', 'multisite')
        registry.refresh()
        r = registry.site_add('shop', 'shop', domains=['shop.example.com', 'www.shop.example.com'], cf_account='customer')
        self.assertTrue(r['created'])
        inv = config.load_inventory(self.inv_file)
        self.assertEqual(inv.site('shop').hosts['node-a'], {'stack': 'shop', 'host': 'shop.example.net'})
        self.assertEqual(inv.site('shop').primary, 'node-a')
        changed = registry.changes(since=r['revision'] - 1)['changes']
        self.assertEqual([(c['op'], c['uid'], c['site']['slug']) for c in changed], [('changed', 'node-a:shop:1', 'shop')])
        with self.assertRaisesRegex(config.ConfigError, 'blog-id'):
            registry.site_add('news', 'net', cf_account='customer')
        with self.assertRaisesRegex(config.ConfigError, 'cf-account'):
            registry.site_add('other', 'net', blog_id=1, host='net.example.net')
        bad = json.loads(self.inv_file.read_text())
        with self.assertRaises(config.ConfigError):            # duplicate domain: rejected, file unchanged
            registry.site_add('dup', 'net', blog_id=1, host='net.example.net', domains=['shop.example.com'], cf_account='customer')
        self.assertEqual(json.loads(self.inv_file.read_text()), bad)
        state.save_overrides({'shop.example.com': {'status': 'active', 'site': 'shop', 'stack': 'shop'}})
        with self.assertRaisesRegex(config.ConfigError, 'override'):
            registry.site_remove('shop')
        state.save_overrides({})
        self.map_site('news', 'net', 'news.net.example.net', blog_id=2)
        raw = json.loads(self.inv_file.read_text())
        raw['sites'][-1]['failover_to'] = 'node-a'
        self.inv_file.write_text(json.dumps(raw))
        with self.assertRaisesRegex(config.ConfigError, 'failover_to'):
            registry.site_remove('news')                        # still hosted on primary, names node-a
        r = registry.site_remove('shop')                        # its only host: dropped
        self.assertTrue(r['dropped_from_inventory'])
        r = registry.site_remove('news', everywhere=True)
        self.assertEqual(config.load_inventory(self.inv_file).sites, [])

    def test_stack_remove_unregisters_archives_and_never_deletes(self):
        self.add_stack('shop')
        self.map_site('shop', 'shop', 'shop.example.net')
        (self.root / 'stacks/shop/data/wordpress/wp-content/uploads/keep.txt').write_text('x')
        siteguard.save_key('shop', {'key': 'k', 'kind': 'node'})
        registry.refresh()
        with self.assertRaisesRegex(config.ConfigError, 'sites mapped'):
            provision.remove('shop')
        with patch.object(provision, 'compose', return_value=subprocess.CompletedProcess([], 0, '', '')) as down:
            r = provision.remove('shop', detach_sites=True)
        self.assertEqual(down.call_args.args[2:], ('down',))
        archived = Path(r['archived_to'])
        self.assertTrue((archived / 'data/wordpress/wp-content/uploads/keep.txt').exists())
        inv = config.load_inventory(self.inv_file)
        self.assertNotIn('shop', inv.origin('node-a').stacks)
        self.assertEqual(list(inv.site('shop').hosts), ['primary'])    # still defined on its primary
        self.assertNotIn('shop', siteguard.load_keys())
        self.assertIn('node-a:shop:1', [c['uid'] for c in registry.changes(0)['changes'] if c['op'] == 'removed'])

    def test_disk_usage_per_stack_and_blog(self):
        self.add_stack('net', 'multisite')
        up = self.root / 'stacks/net/data/wordpress/wp-content/uploads'
        (up / 'sites/2').mkdir(parents=True)
        (up / 'a.bin').write_bytes(b'x' * 10000)
        (up / 'sites/2/b.bin').write_bytes(b'x' * 50000)
        origin = config.load_inventory(self.inv_file).origin('node-a')
        d = registry.disk_usage(origin, 'net', [1, 2])
        self.assertGreaterEqual(d['uploads']['2'], 50000)
        self.assertLess(d['uploads']['1'], 50000)
        self.assertGreaterEqual(d['stack_bytes'], 60000)

    # --------------------------------------------------------------------- API

    def test_agent_serves_registry_changes_and_siteguard_view_with_the_node_token(self):
        self.add_stack('shop')
        self.add_stack('blog')
        with patch.object(agent, 'node_status', return_value={}):
            tok = agent.rotate_token()
            ag = agent.Agent()
            srv = ThreadingHTTPServer(('127.0.0.1', 0), agent.make_handler(ag))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            base = f'http://127.0.0.1:{srv.server_address[1]}'

            def call(path, body=None, token=tok):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                             method='POST' if body is not None else 'GET',
                                             headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return r.status, json.load(r)
                except urllib.error.HTTPError as e:
                    return e.code, json.load(e)
            self.assertEqual(call('/v1/registry', token='wrong')[0], 401)
            code, refreshed = call('/v1/registry/refresh', {})
            self.assertEqual(code, 200, refreshed)
            code, reg = call('/v1/registry')
            self.assertEqual(code, 200)
            self.assertEqual(sorted(s['uid'] for s in reg['sites']), ['node-a:blog:1', 'node-a:shop:1'])
            self.assertTrue(all({'health', 'versions', 'backup', 'disk', 'tunnel', 'overrides'} <= set(s) for s in reg['sites']))
            code, one = call('/v1/registry/sites/node-a:shop:1')
            self.assertEqual((code, one['stack']), (200, 'shop'))
            self.assertEqual(call('/v1/registry/sites/shop:1')[1]['uid'], 'node-a:shop:1')
            self.assertEqual(call('/v1/registry/sites/nope')[0], 404)
            code, feed = call('/v1/registry/changes?since=0')
            self.assertEqual((code, feed['revision'], len(feed['changes'])), (200, 2, 2))
            code, sgp = call('/v1/siteguard/health')
            self.assertEqual((code, sgp['api_version'], sgp['summary']['total']), (200, 2, 2))
            code, evs = call('/v1/events?after=0')
            self.assertEqual([e['kind'] for e in evs['events']].count('registry.site_added'), 2)
            last = evs['events'][-1]['seq']
            self.assertEqual(call(f'/v1/events?after={last}')[1]['events'], [])
            self.assertEqual(call('/v1/registry/sites/add', {'slug': 'x'})[0], 400)
            code, added = call('/v1/registry/sites/add', {'slug': 'shop', 'stack': 'shop', 'cf_account': 'customer'})
            self.assertEqual(code, 200, added)
            code, err = call('/v1/registry/sites/add', {'slug': 'blog', 'stack': 'blog', 'blog_id': True})
            self.assertEqual(code, 400)
            self.assertEqual(call('/v1/registry/refresh', {'stacks': ['nope']})[0], 400)
            self.assertEqual(call('/v1/siteguard/enroll', {'stack': 'nope'})[0], 400)

    def test_node_status_reports_registry_counts(self):
        self.add_stack('shop')
        registry.refresh_health()
        s = ops.registry_summary()
        self.assertEqual((s['sites'], s['status']), (1, {'operational': 1}))

    def test_cli_registry_and_site_commands(self):
        import io
        import wpops.__main__ as cli
        self.add_stack('shop')
        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(list(argv))
            return rc, out.getvalue()
        self.assertEqual(run('--json', 'site', 'add', 'shop', '--stack', 'shop', '--cf-account', 'customer')[0], 0)
        self.assertEqual(run('--json', 'registry', 'refresh')[0], 0)
        rc, out = run('--json', 'registry')
        self.assertEqual(json.loads(out)['sites'][0]['slug'], 'shop')
        rc, out = run('registry')
        self.assertIn('node-a:shop:1', out)
        self.assertEqual(run('site', 'remove', 'shop')[0], 1)          # needs --yes



FAKE_DOCKER = r"""#!/usr/bin/env python3
# Stand-in for `docker compose` in a stack directory: WP-CLI answers come from
# fake-wp.json there; a SiteGuard key set over stdin goes to sg-keys.txt.
import json, os, sys
args = sys.argv[1:]
wp = json.load(open('fake-wp.json')) if os.path.exists('fake-wp.json') else {}
if args[:1] == ['compose'] and 'ps' in args:
    print(json.dumps({'Name': 'nginx-1', 'Service': 'nginx', 'State': 'running', 'Health': 'healthy'}))
    if os.environ.get('FAKE_CRON_STOPPED') != '1':
        print(json.dumps({'Name': 'wp-cron-1', 'Service': 'wp-cron', 'State': 'running', 'Health': ''}))
    sys.exit(0)
if 'wp' not in args:
    sys.exit(0)
if '--entrypoint' in args:
    # compose run --rm --no-deps -T --entrypoint wp wp-cron <wp args>
    w = args[args.index('--entrypoint') + 3:]
else:
    w = args[args.index('wp') + 1:]
if w[:1] == ['eval']:
    print('WPOPS_FACTS:' + json.dumps(wp['facts']))
elif w[:2] == ['siteguardbackup', 'health_api_key'] and '--format=json' in w:
    if not wp['facts'].get('siteguard'):
        sys.exit(1)
    print(json.dumps({'enabled': True, 'key': {'has_key': True}}))
elif w[:2] == ['siteguardbackup', 'health_api_key'] and '--set-node' in w:
    open('sg-keys.txt', 'a').write(sys.stdin.read().strip() + '\n')
    print('Success: node key set.')
else:
    sys.exit(1)
"""


class AgentProcessTests(unittest.TestCase):
    """`wpops serve` as a process on a node directory, its stacks stood in for by
    local HTTP servers and a fake `docker` on PATH: the registry loop, facts,
    SiteGuard enrolment, health and the change feed over the real API."""

    def test_agent_process_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'config').mkdir()
        fakebin = root / 'fakebin'
        fakebin.mkdir()
        (fakebin / 'docker').write_text(FAKE_DOCKER)
        (fakebin / 'docker').chmod(0o755)
        servers = {}
        stacks = {}
        for name, mode, facts in [
                ('shop', 'single', {'wp': '6.9', 'php': '8.3.2', 'home': 'https://shop.example.net/', 'name': 'Shop',
                                    'siteguard': '1.6.4', 'plugins': [{'file': 'siteguardbackup/siteguardbackup.php',
                                                                       'name': 'SiteGuard Backup', 'version': '1.6.4', 'active': True}],
                                    'blogs': [], 'last_backup': 1700000000, 'backup_count': 2}),
                ('net', 'multisite', {'wp': '6.9', 'php': '8.3.2', 'home': 'https://net.example.net/', 'siteguard': None,
                                      'plugins': [], 'blogs': [
                                          {'blog_id': 1, 'domain': 'net.example.net', 'path': '/', 'url': 'https://net.example.net/'},
                                          {'blog_id': 2, 'domain': 'news.net.example.net', 'path': '/', 'url': 'https://news.net.example.net/'}]})]:
            fake = FakeStack()
            self.addCleanup(fake.close)
            path = root / 'stacks' / name
            path.mkdir(parents=True)
            (path / '.env').write_text(f'HTTP_PORT="{fake.port}"\n')
            (path / 'fake-wp.json').write_text(json.dumps({'facts': facts}))
            servers[name] = fake
            stacks[name] = {'path': str(path), 'mode': mode, 'managed': True, 'service': f'http://wpops-{name}:80',
                            'ingress_network': 'wpops-edge'}
        shop = servers['shop']
        shop.sg_sites = [sg_row(1, 'https://shop.example.net/')]

        class KeysFromFile(set):
            def __contains__(self, k):
                f = root / 'stacks/shop/sg-keys.txt'
                return f.exists() and k in f.read_text().split()
        shop.keys = KeysFromFile()
        (root / 'config/inventory.json').write_text(json.dumps({
            'origins': {'node-a': {'kind': 'node', 'stacks': stacks, 'tunnels': {}}},
            'sites': [{'slug': 'shop', 'primary': 'node-a', 'cf_account': 'customer', 'domains': ['shop.example.com'],
                       'hosts': {'node-a': {'stack': 'shop', 'host': 'shop.example.net'}}}]}))
        import socket
        with socket.socket() as so:
            so.bind(('127.0.0.1', 0))
            port = so.getsockname()[1]
        (root / 'config/wpops.json').write_text(json.dumps({'node': 'node-a', 'agent': {'port': port},
                                                             'registry': {'health_timeout': 5}}))
        env = {**os.environ, 'WPOPS_REPO_DIR': str(root), 'PATH': f'{fakebin}:{os.environ["PATH"]}'}
        env.pop('CF_API_URL', None)
        bin_ = str(ROOT / 'scripts/wpops/bin/wpops')
        token = subprocess.run([bin_, 'token', 'rotate'], env=env, capture_output=True, text=True, check=True).stdout.strip()
        log = open(root / 'agent.log', 'w')
        self.addCleanup(log.close)
        proc = subprocess.Popen([bin_, 'serve'], env=env, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(10)))

        def call(path, body=None):
            req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method='POST' if body is not None else 'GET',
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)

        def wait_for(pred, what, timeout=40):
            end = time.time() + timeout
            last = None
            while time.time() < end:
                try:
                    last = call('/v1/registry')
                    if pred(last):
                        return last
                except OSError:
                    pass
                time.sleep(0.5)
            self.fail(f'{what}: {json.dumps(last)[:1500]}')
        # The registry loop collects facts and enrols SiteGuard on its own.
        # Facts are collected one stack after the other: wait for both.
        reg = wait_for(lambda r: any(s['name'] == 'shop' and s['siteguard']['key'] for s in r['stacks'])
                       and len(r['sites']) == 3, 'shop gets a SiteGuard node key and every blog is listed')
        self.assertEqual(sorted(s['uid'] for s in reg['sites']), ['node-a:net:1', 'node-a:net:2', 'node-a:shop:1'])
        call('/v1/registry/refresh', {})
        reg = call('/v1/registry')
        by = {s['uid']: s for s in reg['sites']}
        self.assertEqual(by['node-a:shop:1']['health']['source'], 'siteguard')
        self.assertEqual(by['node-a:shop:1']['slug'], 'shop')
        self.assertEqual(by['node-a:shop:1']['versions']['wordpress'], '6.9')
        self.assertEqual(by['node-a:net:2']['health'], {**by['node-a:net:2']['health'], 'source': 'direct', 'status': 'operational'})
        self.assertIn(('GET', 'news.net.example.net', '/'), [r[:3] for r in servers['net'].requests])
        rev = reg['revision']
        cursor = max(e['seq'] for e in call('/v1/events?after=0')['events'])
        # A new blog appears in the network: the change feed and events report it.
        f = json.loads((root / 'stacks/net/fake-wp.json').read_text())
        f['facts']['blogs'].append({'blog_id': 3, 'domain': 'arts.net.example.net', 'path': '/', 'url': 'https://arts.net.example.net/'})
        (root / 'stacks/net/fake-wp.json').write_text(json.dumps(f))
        job = call('/v1/registry/refresh', {'facts': True, 'stacks': ['net']})['job']
        end = time.time() + 30
        while call(f'/v1/jobs/{job}')['status'] not in ('succeeded', 'failed') and time.time() < end:
            time.sleep(0.3)
        feed = call(f'/v1/registry/changes?since={rev}')
        self.assertEqual([(c['op'], c['uid']) for c in feed['changes']], [('added', 'node-a:net:3')])
        evs = call(f'/v1/events?after={cursor}')['events']
        self.assertIn('registry.site_added', [e['kind'] for e in evs])
        sgp = call('/v1/siteguard/health')
        self.assertEqual(sgp['summary']['total'], 4)
        status = call('/v1/status')
        self.assertEqual(status['registry']['sites'], 4)


if __name__ == '__main__':
    unittest.main()
