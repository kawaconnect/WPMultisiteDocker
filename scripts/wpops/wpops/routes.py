"""Publish native primary and standby hostnames to the correct stack.

Existing records pointing elsewhere are refused. Moving production traffic
between origins belongs to failover, which records reversible overrides.
"""
from .cf import Cloudflare, CfError
from .config import ConfigError, cf_accounts
from .failover import write_aliases
from . import state


def plan(inv, node, accounts=None):
    accounts = accounts if accounts is not None else cf_accounts()
    origin = inv.origin(node)
    clients = {n: Cloudflare(a.token, a.account_id, a.name) for n, a in accounts.items()}
    rows, refused, seen = [], [], set()
    for site in inv.sites:
        host = site.host_on(node)
        if not host:
            continue
        # A staging name (host differs from the siteurl host) never carries
        # production traffic, so it is published even while the site is
        # disabled; `enabled: false` keeps gating production domains and failover.
        staging = site.site_host_on(node) != host['host'].lower()
        if not site.enabled and not staging:
            continue
        names = list(dict.fromkeys([host['host'], *(site.domains if site.enabled and site.primary == node else [])]))
        for domain in names:
            if domain in seen:
                continue
            seen.add(domain)
            if domain in state.overrides():
                refused.append({'domain': domain, 'reason': 'active override; use failover rollback first'})
                continue
            candidates = ([site.cf_account] if domain in site.domains else
                          [n for n, a in accounts.items() if a.standby] + [site.cf_account])
            account, zone = None, None
            for name in dict.fromkeys(candidates):
                if name not in clients:
                    continue
                try:
                    zone = clients[name].zone_id(domain)
                    account = name
                    break
                except CfError:
                    continue
            if not account:
                refused.append({'domain': domain, 'reason': 'no configured account has access to this zone'})
                continue
            tid = origin.tunnels.get(account)
            if not tid:
                refused.append({'domain': domain, 'reason': f'no tunnel for {account}; run wpops tunnels ensure'})
                continue
            target = f'{tid}.cfargotunnel.com'
            cf = clients[account]
            recs = [r for r in cf.dns_records(zone, name=domain) if r.get('type') in ('A', 'AAAA', 'CNAME')]
            if recs and not all(r['type']=='CNAME' and r['content'].rstrip('.').lower()==target for r in recs):
                refused.append({'domain': domain, 'reason': 'DNS already points elsewhere; use failover or migrate DNS explicitly'})
                continue
            from .stacks import ingress_service
            rows.append({'domain': domain, 'account': account, 'zone': zone, 'tunnel': tid,
                         'stack': host['stack'], 'service': ingress_service(origin, host['stack']),
                         'target': target, 'create_dns': not bool(recs)})
    return {'node': node, 'routes': rows, 'refused': refused}


def apply(inv, node, accounts=None):
    accounts = accounts if accounts is not None else cf_accounts()
    with state.locked('failover'):
        p = plan(inv, node, accounts)
        if p['refused']:
            raise ConfigError('routing refused: ' + str(p['refused']))
        from .stacks import ps
        origin = inv.origin(node)
        for stack in {r['stack'] for r in p['routes']}:
            containers = ps(origin, stack)
            if not any(c['service']=='nginx' and c['state']=='running' and c.get('health') in ('healthy', '', None)
                       for c in containers):
                raise ConfigError(f'stack {stack}: nginx is not ready')
        write_aliases(inv, node)
        for row in p['routes']:
            acct = accounts[row['account']]
            cf = Cloudflare(acct.token, acct.account_id, acct.name)
            cf.backup_zone(row['domain'])
            cf.backup_tunnel(row['tunnel'])
            cf.merge_tunnel_ingress(row['tunnel'], [{'hostname': row['domain'], 'service': row['service']}])
            if row['create_dns']:
                cf.create_record(row['zone'], {'type':'CNAME', 'name': row['domain'], 'content':row['target'],
                                               'proxied': True, 'ttl': 1})
        state.audit('local', 'routes.apply', p)
    return p
