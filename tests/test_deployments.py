"""Offline regression checks. No Docker daemon, Cloudflare writes or real site data."""
import contextlib
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/wpops'))
from wpops import backup, config, failover, provision, restore, routes, stacks, state, tunnels

REAL_PORT_IN_USE = provision.port_in_use


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        self.file = self.root / 'config/inventory.json'
        self.raw = {'origins': {'primary': {'kind':'external', 'targets':[{'type':'A','content':'192.0.2.1'}]},
                               'backup': {'kind':'node', 'stacks':{}, 'tunnels':{'customer':'tid', 'standby':'sid'}}}, 'sites':[]}
        self.save()
        shutil.copy(ROOT / 'docker-compose.stack.yml', self.root)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        for obj, key, val in [
            (provision,'REPO_DIR',self.root), (provision,'INVENTORY_FILE',self.file),
            (tunnels,'REPO_DIR',self.root), (tunnels,'INVENTORY_FILE',self.file),
            (config,'NODE_CONFIG_FILE',self.root/'config/wpops.json'),
            (state,'STATE_DIR',self.root/'state'), (state,'LOCK_DIR',self.root/'state/locks'),
            (state,'OVERRIDES_FILE',self.root/'state/overrides.json'), (state,'AUDIT_LOG',self.root/'state/audit.log')]:
            self.patches.enter_context(patch.object(obj,key,val))
        (self.root/'config/wpops.json').write_text(json.dumps({'node':'backup'}))
        for obj in (provision,tunnels):
            self.patches.enter_context(patch.object(obj,'load_inventory',lambda: config.load_inventory(self.file)))
        self.patches.enter_context(patch('wpops.notify.notify'))
        self.patches.enter_context(patch('wpops.notify.record_event'))
        # Tests must not depend on which loopback ports this machine happens to use.
        self.patches.enter_context(patch.object(provision,'port_in_use',return_value=False))

    def save(self):
        self.file.write_text(json.dumps(self.raw))

    def add_stack(self, name, mode='single'):
        self.raw['origins']['backup']['stacks'][name] = {
            'path':str(self.root/name), 'mode':mode, 'service':f'http://wpops-{name}:80',
            'ingress_network':'wpops-edge','managed':True}
        (self.root/name/'data/wordpress/wp-content').mkdir(parents=True)

    def add_site(self, slug, stack, blog_id=None):
        host={'stack':stack,'host':f'{slug}.standby.example'}
        if blog_id: host['blog_id']=blog_id
        self.raw['sites'].append({'slug':slug,'primary':'primary','cf_account':'customer',
                                 'domains':[f'{slug}.example',f'www.{slug}.example'],
                                 'hosts':{'primary':{'host':f'{slug}.example'},'backup':host}})

    def inventory(self):
        self.save()
        return config.load_inventory(self.file)

    @unittest.skipUnless(shutil.which('docker'), 'needs the Docker CLI with Compose v2 (no daemon)')
    def test_all_requested_topologies_and_compose(self):
        for topology in [('single','single'), ('multisite',), ('single','multisite','multisite')]:
            for index, mode in enumerate(topology):
                name=f'{mode}-{len(topology)}-{index}'
                # No Docker/network operations are needed to create configuration.
                with patch.object(subprocess,'run',side_effect=AssertionError('create must be offline')):
                    result=provision.create(name,mode,f'{name}.example',18080+len(list((self.root/'stacks').iterdir())) if (self.root/'stacks').exists() else 18080)
                stack=Path(result['path'])
                env={k:v for k,v in os.environ.items() if not k.startswith('COMPOSE_') and k!='WPOPS_REPO_DIR'}
                r=subprocess.run(['docker','compose','config','--format','json'],cwd=stack,env=env,text=True,capture_output=True)
                self.assertEqual(r.returncode,0,r.stderr)
                doc=json.loads(r.stdout)
                self.assertEqual(doc['name'],f'wps-{name}')
                services=doc['services']
                self.assertEqual(services['wordpress']['environment']['WP_MODE'],mode)
                self.assertEqual(services['wordpress']['environment'], services['wp-cron']['environment'])
                self.assertNotIn('edge',services['mariadb']['networks'])
                self.assertNotIn('edge',services['wordpress']['networks'])
                self.assertEqual(services['nginx']['networks']['edge']['aliases'],[f'wpops-{name}'])
                self.assertTrue(any(v['target']=='/backups' and v['read_only'] for v in services['wp-cron']['volumes']))
                self.assertTrue(any(v['target']=='/etc/nginx/wpops' for v in services['nginx']['volumes']))
                self.assertFalse(any(v['target'].startswith('/var/www/html/wp-content/') for v in services['wordpress']['volumes']))
                self.assertEqual((stack/'.env').stat().st_mode & 0o777,0o600)

    def test_ports_names_and_modes_rejected(self):
        for name,mode,domain,port in [('../x','single','x.example',18081),('x','other','x.example',18081),
                                      ('x','single',"bad'host",18081),('x','single','x.example',80)]:
            with self.assertRaises(config.ConfigError): provision.create(name,mode,domain,port)
        provision.create('one','single','one.example',18081)
        with self.assertRaises(config.ConfigError): provision.create('two','single','two.example',18081)

    def test_examples_validate(self):
        config.load_inventory(ROOT/'config/inventory.example.json')

    def test_inventory_rejects_shared_single_and_duplicate_domains(self):
        self.add_stack('one');self.add_site('one','one');self.add_site('two','one')
        with self.assertRaisesRegex(config.ConfigError,'multiple sites'): self.inventory()
        self.raw['sites'].pop()
        self.add_stack('two');self.add_site('two','two')
        self.raw['sites'][1]['domains']=['one.example']
        with self.assertRaisesRegex(config.ConfigError,'multiple sites'): self.inventory()

    def test_distinct_backup_endpoints_on_one_origin_and_legacy(self):
        self.add_stack('one');self.add_stack('two','multisite')
        self.raw['origins']['primary']['sync_api']='https://legacy.example'
        self.raw['backup_sources']={n:{'origin':'backup','stack':n,'sync_api':f'https://{n}.example'} for n in ('one','two')}
        inv=self.inventory()
        with patch.object(backup,'source_key',return_value='secret'), patch.object(backup,'SyncClient') as client:
            backup.client_for(inv,'one');backup.client_for(inv,'two');backup.client_for(inv,'primary')
            self.assertEqual([c.args[0] for c in client.call_args_list],['https://one.example','https://two.example','https://legacy.example'])
        with patch.object(backup,'node_config',return_value={'backup':{'sources':[]}}):
            self.assertEqual(set(backup.sources_to_pull(inv)),{'one','two','primary'})

    def test_batch_restore_cannot_target_wrong_stack(self):
        self.add_stack('net','multisite');self.add_stack('other','multisite');self.add_site('one','net',3)
        inv=self.inventory()
        man={'source':'profile','source_origin':'primary','sites':[{'domain':'one.example','blog_id':10}]}
        self.assertEqual(restore.build_batch_map(inv,man,'backup',['one'],'net')[0]['target_blog_id'],3)
        with self.assertRaisesRegex(restore.RestoreError,'different target stack'):
            restore.build_batch_map(inv,man,'backup',['one'],'other')

    def test_custom_login_predeployment_and_per_blog_policy(self):
        provision.create('net','multisite','net.example',18081)
        with patch.object(provision,'install_login_plugin'):
            provision.set_login('net','staff-entrance')
            provision.set_login('net','shop-staff',2)
        policy=json.loads((self.root/'stacks/net/policy/login-paths.json').read_text())
        self.assertEqual(policy['default'],'staff-entrance')
        self.assertEqual(policy['blogs']['2'],'shop-staff')
        locations=(self.root/'stacks/net/nginx/login.conf').read_text()
        self.assertIn('limit_req zone=wp_login',locations)
        self.assertIn('shop',locations)
        for bad in ['wp-admin','wp-login.php','x/y',"x';exit",'wp-json']:
            with self.assertRaises(config.ConfigError): provision.login_slug(bad)

    @unittest.skipUnless(os.environ.get('WPOPS_TEST_PHP') or shutil.which('php'), 'PHP CLI is optional for the policy hook check')
    def test_php_policy_applies_defaults_overrides_and_activation(self):
        policy=self.root/'policy.json'
        policy.write_text(json.dumps({'enabled':True,'default':'staff-door','blogs':{'2':'news-editors'}}))
        script=self.root/'policy-test.php'
        script.write_text("<?php\n" +
            "define('ABSPATH', '/'); define('WPOPS_LOGIN_POLICY_FILE', " + json.dumps(str(policy)) + ");\n" +
            "function add_filter(...$args) {} function add_action(...$args) {} function get_current_blog_id() { return $GLOBALS['blog']; } function is_multisite() { return false; }\n" +
            "require " + json.dumps(str(ROOT/'plugin/wpops-policy.php')) + ";\n" +
            "$GLOBALS['blog']=1; $one=wpops_login_slug(false); $GLOBALS['blog']=2; $two=wpops_login_slug(false); $GLOBALS['blog']=3; $new=wpops_login_slug(false);\n" +
            "echo json_encode([$one,$two,$new,wpops_login_plugins(['existing/plugin.php'])]);")
        r=subprocess.run([os.environ.get('WPOPS_TEST_PHP') or shutil.which('php'),'-n',str(script)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),['staff-door','news-editors','staff-door',['existing/plugin.php','wps-hide-login/wps-hide-login.php']])

    def test_sibling_failover_routes_before_dns_and_rollback_exactly(self):
        self.add_stack('one');self.add_stack('net','multisite');self.add_site('one','one');self.add_site('shop','net',2)
        inv=self.inventory();cfg={'node':'backup','failover':{'default_to':'backup','tunnel_service':'http://nginx:80'}}
        dns={}
        for s in inv.sites:
            dns[s.domains[0]]=[{'id':s.slug+'-a','type':'A','content':'192.0.2.1','name':s.domains[0],'ttl':300,'proxied':True}]
            dns[s.domains[1]]=[{'id':s.slug+'-w','type':'CNAME','content':s.domains[0],'name':s.domains[1],'ttl':300,'proxied':True}]
        original=copy.deepcopy(dns);events=[];rolling_back=[False]
        class CF:
            def zone_id(self,d): return 'zone'
            def dns_records(self,z,name): return copy.deepcopy(dns.get(name,[]))
            def backup_zone(self,d): pass
            def backup_tunnel(self,t): pass
            def merge_tunnel_ingress(self,t,rules): events.append(('ingress',rules))
            def remove_tunnel_ingress_hosts(self,t,hosts): events.append(("remove-ingress",hosts))
            def delete_record(self,z,id):
                aliases=json.loads(failover.alias_file(inv.origin('backup'),'one').read_text())['aliases']
                if not rolling_back[0]:
                    self_test.assertIn('www.one.example',aliases)
                events.append(('delete',id))
                for k in dns: dns[k]=[r for r in dns[k] if r['id']!=id]
            def create_record(self,z,r):
                item={**r,'id':'new-'+r['name']};dns.setdefault(r['name'],[]).append(item);return item
        self_test=self
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        with patch.object(failover,'Cloudflare',return_value=CF()), patch.object(stacks,'ps',return_value=[{'service':x,'state':'running','health':'healthy'} for x in ('wordpress','nginx')]):
            p=failover.plan(inv,inv.sites,cfg=cfg,accounts=accounts)
            self.assertEqual(len(p.changes),4)
            self.assertEqual({c.service for c in p.changes},{'http://wpops-one:80','http://wpops-net:80'})
            result=failover.apply(inv,p,'test','backup',accounts,log=lambda _:None)
            self.assertEqual(result['failed'],[])
            self.assertEqual(events[0][0],'ingress')
            aliases=json.loads(failover.alias_file(inv.origin('backup'),'net').read_text())['aliases']
            self.assertEqual(aliases['www.shop.example']['blog_id'],2)
            events.append(('rollback',None))
            rolling_back[0]=True
            partial=failover.rollback(inv,'test','backup',domains=['www.one.example'],accounts=accounts,log=lambda _:None)
            self.assertEqual(partial['failed'],[])
            self.assertFalse(any(e[0]=='remove-ingress' for e in events))
            result=failover.rollback(inv,'test','backup',all_sites=True,accounts=accounts,log=lambda _:None)
            self.assertEqual(result['failed'],[])
        self.assertEqual(state.overrides(),{})
        for name,recs in dns.items():
            self.assertEqual([{k:v for k,v in r.items() if k!='id'} for r in recs], [{k:v for k,v in r.items() if k!='id'} for r in original[name]])

    def test_ingress_failure_does_not_change_dns(self):
        self.add_stack('one');self.add_site('one','one');inv=self.inventory()
        change=failover.Change('one','one.example','customer','zone','primary','backup','one','http://wpops-one:80','tid',[],{'type':'CNAME'})
        from unittest.mock import MagicMock
        cf=MagicMock();cf.merge_tunnel_ingress.side_effect=Exception('unreachable')
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        with patch.object(failover,'Cloudflare',return_value=cf), patch.object(stacks,'ps',return_value=[{'service':x,'state':'running','health':'healthy'} for x in ('wordpress','nginx')]):
            result=failover.apply(inv,failover.Plan('backup',[change]),'test','backup',accounts,log=lambda _:None)
            self.assertTrue(result['failed']);cf.delete_record.assert_not_called();cf.create_record.assert_not_called()
        with self.assertRaises(failover.FailoverError):
            failover.apply(inv,failover.Plan('backup',[change]),'test','other',accounts)

    def test_failed_dns_creation_can_retry_without_losing_original_records(self):
        self.add_stack('one');self.add_site('one','one');self.raw['sites'][0]['domains']=['one.example'];inv=self.inventory()
        from unittest.mock import MagicMock
        cf=MagicMock();cf.zone_id.return_value='zone'
        record={'id':'original','type':'A','content':'192.0.2.1','name':'one.example','ttl':300,'proxied':True}
        dns=[record.copy()]
        cf.dns_records.side_effect=lambda *a,**kw:copy.deepcopy(dns)
        cf.delete_record.side_effect=lambda *a:dns.clear()
        def create(*args):
            if not getattr(create,'failed',False):
                create.failed=True
                raise failover.CfError('simulated create failure')
            dns.append({**args[1],'id':'new'})
            return dns[-1]
        cf.create_record.side_effect=create
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        cfg={'node':'backup','failover':{'default_to':'backup','tunnel_service':'http://nginx:80'}}
        with patch.object(failover,'Cloudflare',return_value=cf), patch.object(stacks,'ps',return_value=[{'service':x,'state':'running','health':'healthy'} for x in ('wordpress','nginx')]):
            p=failover.plan(inv,inv.sites,cfg=cfg,accounts=accounts)
            result=failover.apply(inv,p,'test','backup',accounts,log=lambda _:None)
            # The name never stays empty: the deleted record is put back.
            self.assertTrue(result['failed'])
            self.assertEqual([(r['type'],r['content']) for r in dns],[('A','192.0.2.1')])
            retry=failover.plan(inv,inv.sites,cfg=cfg,accounts=accounts)
            self.assertEqual(len(retry.changes),1)
            result=failover.apply(inv,retry,'test','backup',accounts,log=lambda _:None)
            self.assertEqual(result['failed'],[])
            self.assertEqual(state.overrides()['one.example']['original_records'],[record])

    def test_restore_reapplies_login_runtime_and_preserves_destination_policy(self):
        provision.create('one','single','one.example',18081)
        inv=config.load_inventory(self.file);origin=inv.origin('backup');path=Path(origin.stacks['one']['path'])
        wc=path/'data/wordpress/wp-content';wc.mkdir(parents=True)
        source=path/'managed-plugins/wps-hide-login/wps-hide-login.php';source.write_text('<?php // verified fixture')
        policy=path/'policy/login-paths.json';policy.write_text(json.dumps({'enabled':True,'default':'staff-door','blogs':{}}))
        before=policy.read_bytes();setdir=self.root/'backup-set';setdir.mkdir()
        man={'source':'shop-source','nonce':'abc','files':[],'pulled_at':1}
        def wp_call(origin,name,*args,**kwargs):
            if args[:2]==('siteguardbackup','restore'):
                shutil.rmtree(wc/'plugins',ignore_errors=True)
                (wc/'wpops-host-aliases.json').write_text('{"aliases":{"wrong.example":{}}}')
                return subprocess.CompletedProcess([],0,'Restore complete','')
            return subprocess.CompletedProcess([],0,'imported','')
        job=type('Job',(),{'log':staticmethod(lambda _:None),'set_progress':lambda *a,**k:None,'actor':'test','id':'job'})()
        with patch.object(backup,'manifest',return_value=(setdir,man)), patch.object(backup,'save_manifest'), patch.object(restore,'node_config',return_value={}), patch.object(restore.disk,'require'), patch.object(restore,'require_free'), patch.object(restore,'_fix_content_ownership'), patch.object(restore,'wp',side_effect=wp_call), patch.object(restore,'_parse_import_timestamp',return_value=123), patch.object(stacks,'compose') as compose:
            restore.apply(job,inv,'backup','shop-source/set','one',all_sites=True)
        # PHP-FPM is restarted, not reloaded with USR2: Docker does not bring a
        # re-executed php-fpm back after a daemon or host restart.
        calls=[c.args[2:] for c in compose.call_args_list]
        self.assertIn(('restart','wordpress'),calls)
        self.assertFalse(any('USR2' in c for c in calls))
        self.assertEqual(policy.read_bytes(),before)
        self.assertEqual((wc/'plugins/wps-hide-login/wps-hide-login.php').read_bytes(),source.read_bytes())
        self.assertEqual(json.loads((wc/'wpops-host-aliases.json').read_text())['aliases'],{})

    @unittest.skipUnless(shutil.which('docker'), 'needs the Docker CLI with Compose v2 (no daemon)')
    def test_tunnels_without_main_and_multiple_legacy_networks(self):
        self.add_stack('one');self.add_stack('net','multisite')
        self.raw['origins']['backup']['stacks']['legacy']={'path':str(self.root/'legacy'),'mode':'multisite','service':'http://nginx:80'}
        self.save()
        from unittest.mock import MagicMock
        cf=MagicMock();cf.verify_token.return_value={'status':'active'};cf.tunnel.return_value={'id':'tid'};cf.tunnel_token.return_value='fake-token'
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        with patch.object(tunnels,'Cloudflare',return_value=cf), patch.object(tunnels,'cf_accounts',return_value=accounts),patch.object(tunnels,'load_env',return_value={}):
            tunnels.ensure(start=False,log=lambda _:None)
        file=self.root/'docker-compose.wpops-tunnels.json';doc=json.loads(file.read_text())
        self.assertEqual({n['name'] for n in doc['networks'].values()},{'wpops-edge','legacy_frontend'})
        self.assertIn('cf-backup',doc['services'])
        self.assertEqual(file.stat().st_mode & 0o777,0o600)
        r=subprocess.run(['docker','compose','-f',str(file),'config','--quiet'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_routes_use_distinct_services_and_refuse_foreign_dns(self):
        self.add_stack('one');self.add_stack('net','multisite');self.add_site('one','one');self.add_site('news','net',2)
        inv=self.inventory()
        from unittest.mock import MagicMock
        cf=MagicMock();cf.zone_id.return_value='zone';cf.dns_records.return_value=[]
        accounts={'customer':config.CfAccount(1,'customer','acct','secret'),
                  'standby':config.CfAccount(2,'standby','acct2','secret2',True)}
        with patch.object(routes,'Cloudflare',return_value=cf):
            p=routes.plan(inv,'backup',accounts)
            self.assertEqual(p['refused'],[])
            self.assertEqual({r['service'] for r in p['routes']},{'http://wpops-one:80','http://wpops-net:80'})
            self.assertTrue(all(r['account']=='standby' for r in p['routes']))
            cf.create_record.assert_not_called()
            cf.dns_records.return_value=[{'type':'A','content':'192.0.2.44'}]
            with self.assertRaisesRegex(config.ConfigError,'routing refused'): routes.apply(inv,'backup',accounts)
            cf.merge_tunnel_ingress.assert_not_called();cf.create_record.assert_not_called()

    def test_staging_host_is_an_alias_of_the_production_siteurl(self):
        self.add_stack('one');self.add_stack('net','multisite');self.add_stack('plain')
        self.add_site('one','one');self.add_site('news','net',2);self.add_site('plain','plain')
        self.raw['sites'][0]['hosts']['backup'].update(host='one-backup.standby.example',site_host='one.example')
        self.raw['sites'][1]['hosts']['backup'].update(host='news-backup.standby.example',site_host='news.example')
        inv=self.inventory()
        self.assertEqual(inv.site('one').site_host_on('backup'),'one.example')
        self.assertEqual(inv.site('plain').site_host_on('backup'),'plain.standby.example')
        failover.write_aliases(inv,'backup')
        read=lambda st: json.loads(failover.alias_file(inv.origin('backup'),st).read_text())['aliases']
        self.assertEqual(read('one'),{'one-backup.standby.example':{'site':'one','host':'one.example'}})
        self.assertEqual(read('net'),{'news-backup.standby.example':{'site':'news','host':'news.example','blog_id':2}})
        # host == siteurl host: nothing to alias, exactly as before
        self.assertEqual(read('plain'),{})
        # primary on this node: production domains are the siteurl, only www needs the alias
        self.raw['sites'][0]['primary']='backup'
        inv=self.inventory();failover.write_aliases(inv,'backup')
        self.assertEqual(set(read('one')),{'one-backup.standby.example','www.one.example'})
        # the staging name is what routes publish, in the standby zone
        from unittest.mock import MagicMock
        cf=MagicMock();cf.zone_id.return_value='zone';cf.dns_records.return_value=[]
        accounts={'customer':config.CfAccount(1,'customer','acct','secret'),
                  'standby':config.CfAccount(2,'standby','acct2','secret2',True)}
        self.raw['sites'][0]['primary']='primary';inv=self.inventory()
        with patch.object(routes,'Cloudflare',return_value=cf):
            p=routes.plan(inv,'backup',accounts)
        self.assertIn('one-backup.standby.example',{r['domain'] for r in p['routes']})
        self.assertNotIn('one.example',{r['domain'] for r in p['routes']})
        # disabled (prepared, not cut over): the staging name is still published and
        # aliased, production domains are not, even with this node as primary
        self.raw['sites'][0].update(enabled=False,primary='backup');self.raw['sites'][2]['enabled']=False
        inv=self.inventory();failover.write_aliases(inv,'backup')
        self.assertEqual(read('one'),{'one-backup.standby.example':{'site':'one','host':'one.example'}})
        with patch.object(routes,'Cloudflare',return_value=cf):
            p=routes.plan(inv,'backup',accounts)
        names={r['domain'] for r in p['routes']}
        self.assertTrue({'one-backup.standby.example','plain.standby.example'} <= names)
        self.assertFalse(names & {'one.example','www.one.example'})
        self.raw['sites'][0]['hosts']['backup']['site_host']='not a host'
        with self.assertRaisesRegex(config.ConfigError,'invalid site_host'): self.inventory()

    def test_default_staging_names_override_length_and_collisions(self):
        self.add_stack('one');self.add_stack('two');self.add_stack('net','multisite')
        self.add_site('one','one');self.add_site('two','two');self.add_site('news','net',2)
        for site in self.raw['sites']:
            site['hosts']['backup'].pop('host')
        self.raw['sites'][0]['domains']=['www.Shop.Example.com','shop.example.com']
        self.raw['origins']['backup'].update(standby_domain='standby.example', staging_suffix='b1')
        inv=self.inventory()
        self.assertEqual(inv.site('one').host_on('backup')['host'],'shopexamplecom-b1.standby.example')
        self.assertTrue(inv.site('one').host_on('backup')['host_default'])
        self.assertEqual(inv.site('two').host_on('backup')['host'],'twoexample-b1.standby.example')
        self.assertEqual(inv.site('news').host_on('backup')['host'],'newsexample-b1.standby.example')
        self.assertNotIn('host',self.raw['sites'][0]['hosts']['backup'])      # raw inventory untouched
        # suffix defaults to the node name
        del self.raw['origins']['backup']['staging_suffix']
        self.assertEqual(self.inventory().site('two').host_on('backup')['host'],'twoexample-backup.standby.example')
        # explicit host wins
        self.raw['sites'][1]['hosts']['backup']['host']='custom.standby.example'
        inv=self.inventory()
        self.assertEqual(inv.site('two').host_on('backup')['host'],'custom.standby.example')
        self.assertNotIn('host_default',inv.site('two').host_on('backup'))
        # collisions are refused with the fix in the message
        self.raw['sites'][1]['hosts']['backup']['host']='newsexample-backup.standby.example'
        with self.assertRaisesRegex(config.ConfigError,'collides with site'): self.inventory()
        del self.raw['sites'][1]['hosts']['backup']['host']
        # labels over 63 characters are refused for the default and for overrides
        self.raw['sites'][1]['domains']=['a'*60+'.example']
        with self.assertRaisesRegex(config.ConfigError,'default staging name label .* longer than 63'): self.inventory()
        self.raw['sites'][1]['hosts']['backup']['host']='b'*64+'.standby.example'
        with self.assertRaisesRegex(config.ConfigError,'host label .* longer than 63'): self.inventory()
        self.raw['sites'][1]['hosts']['backup']['host']='two.standby.example'
        # without a standby domain there is no default: host stays required
        del self.raw['origins']['backup']['standby_domain']
        with self.assertRaisesRegex(config.ConfigError,'host is required'): self.inventory()
        self.raw['origins']['backup']['standby_domain']='standby.example'
        with self.assertRaisesRegex(config.ConfigError,'staging_suffix'):
            self.raw['origins']['backup']['staging_suffix']='Not Valid';self.inventory()
        self.raw['origins']['backup']['staging_suffix']='b1'
        # routes publish the default names; staging_only never includes production domains
        self.raw['sites'][0]['primary']='backup'
        inv=self.inventory()
        from unittest.mock import MagicMock
        cf=MagicMock();cf.zone_id.return_value='zone';cf.dns_records.return_value=[]
        accounts={'customer':config.CfAccount(1,'customer','acct','secret'),
                  'standby':config.CfAccount(2,'standby','acct2','secret2',True)}
        with patch.object(routes,'Cloudflare',return_value=cf):
            full={r['domain'] for r in routes.plan(inv,'backup',accounts)['routes']}
            only={r['domain'] for r in routes.plan(inv,'backup',accounts,staging_only=True)['routes']}
        self.assertEqual(only,{'shopexamplecom-b1.standby.example','two.standby.example','newsexample-b1.standby.example'})
        self.assertEqual(full-only,{'www.shop.example.com','shop.example.com'})

    def test_legacy_stack_is_addressed_by_container_name_next_to_other_stacks(self):
        # Alone on a node, the legacy stack keeps its configured service.
        self.raw['origins']['backup']['stacks']['legacy']={'path':str(self.root/'WordPress.Multisite'),'mode':'multisite','service':'http://nginx:80'}
        self.add_site('main','legacy',1)
        inv=self.inventory()
        self.assertEqual(stacks.ingress_service(inv.origin('backup'),'legacy'),'http://nginx:80')
        # Next to a managed stack, every Nginx on the edge network answers to
        # "nginx", so the legacy route must name its own container.
        self.add_stack('one');self.add_site('one','one')
        inv=self.inventory()
        self.assertEqual(stacks.ingress_service(inv.origin('backup'),'legacy'),'http://wordpressmultisite-nginx-1:80')
        self.assertEqual(stacks.ingress_service(inv.origin('backup'),'one'),'http://wpops-one:80')
        from unittest.mock import MagicMock
        cf=MagicMock();cf.zone_id.return_value='zone';cf.dns_records.return_value=[]
        accounts={'customer':config.CfAccount(1,'customer','acct','secret'),
                  'standby':config.CfAccount(2,'standby','acct2','secret2',True)}
        with patch.object(routes,'Cloudflare',return_value=cf):
            p=routes.plan(inv,'backup',accounts)
        self.assertEqual({r['stack']:r['service'] for r in p['routes']},
                         {'legacy':'http://wordpressmultisite-nginx-1:80','one':'http://wpops-one:80'})
        cfg={'node':'backup','failover':{'default_to':'backup','tunnel_service':'http://nginx:80'}}
        dns={'main.example':[{'id':'a','type':'A','content':'192.0.2.1','name':'main.example'}]}
        cf.dns_records.side_effect=lambda z,name:copy.deepcopy(dns.get(name,[]))
        with patch.object(failover,'Cloudflare',return_value=cf):
            p=failover.plan(inv,[inv.site('main')],cfg=cfg,accounts={'customer':accounts['customer']})
        self.assertEqual({c.service for c in p.changes},{'http://wordpressmultisite-nginx-1:80'})

    @unittest.skipUnless(shutil.which('docker'), 'needs the Docker CLI with Compose v2 (no daemon)')
    def test_legacy_root_config_is_shared_with_cli_in_both_modes(self):
        for mode in ('single','multisite'):
            r=subprocess.run(['docker','compose','-f',str(ROOT/'docker-compose.yml'),
                              '--env-file',str(ROOT/'.env.example'),'config','--format','json'],
                             env={**os.environ,'WP_MODE':mode},capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            doc=json.loads(r.stdout)['services']
            self.assertEqual(doc['wordpress']['environment']['WORDPRESS_CONFIG_EXTRA'],
                             doc['wp-cron']['environment']['WORDPRESS_CONFIG_EXTRA'])
            # The mode reaches PHP through the environment, not pasted into the source.
            self.assertIn("getenv('WP_MODE')",doc['wordpress']['environment']['WORDPRESS_CONFIG_EXTRA'])
            self.assertEqual(doc['wordpress']['environment']['WP_MODE'],mode)
            self.assertEqual(doc['wp-cron']['environment']['WP_MODE'],mode)

    def test_agent_accepts_backup_profile_names(self):
        from wpops.agent import Agent
        self.raw['backup_sources']={'shop-source':{'origin':'primary','sync_api':'https://shop.example'}}
        inv=self.inventory();agent=Agent.__new__(Agent);agent.inv=inv
        agent.cfg={'node':'backup'};agent.reload=lambda:None
        from unittest.mock import MagicMock
        with patch('wpops.agent.jobs.Job',return_value=MagicMock(id='job')) as job, patch('wpops.agent.jobs.start_background'), patch('wpops.agent.jobs.active',return_value=False):
            result=agent.route('POST','/v1/backups/pull',{}, {'source':'shop-source'},'test')
            self.assertEqual(result,{'job':'job'})
            self.assertEqual(job.call_args.args[1]['source'],'shop-source')
        with patch.object(backup,'set_schedule',return_value={'ok':True}) as schedule:
            agent.route('POST','/v1/backups/schedule',{}, {'sources':['shop-source']},'test')
            self.assertEqual(schedule.call_args.args[2],['shop-source'])

    def test_setup_bootstraps_empty_inventory_for_chosen_node(self):
        fresh=self.root/'fresh';fresh.mkdir()
        r=subprocess.run([str(ROOT/'scripts/wpops/bin/wpops'),'setup','--node','fresh-node'],
                         env={**os.environ,'WPOPS_REPO_DIR':str(fresh)},capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads((fresh/'config/inventory.json').read_text())['origins'],
                         {'fresh-node':{'kind':'node','stacks':{},'tunnels':{}}})

    @unittest.skipUnless(shutil.which('jq'), 'jq is optional for the shell backup check')
    def test_shell_backup_resolves_inventory_tunnels_and_legacy_fallback(self):
        script = r'''source "$1/scripts/cf-common.sh"
PROJECT_DIR="$2"
cf_api() { printf '{"result":{"id":"%s"}}\n' "$2"; }
get_tunnel_by_name() { printf '{"id":"%s"}\n' "$3"; }
get_configured_tunnel acct dummy customer
'''
        r=subprocess.run(['bash','-eu','-c',script,'test',str(ROOT),str(self.root)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['id'],'/accounts/acct/cfd_tunnel/tid')
        (self.root/'config/wpops.json').unlink()
        r=subprocess.run(['bash','-eu','-c',script,'test',str(ROOT),str(self.root)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['id'],'wp-docker-customer')

    def test_parent_compose_settings_do_not_cross_stack_boundaries(self):
        self.add_stack('one');inv=self.inventory()
        with patch.dict(os.environ,{'COMPOSE_PROJECT_NAME':'wrong','COMPOSE_FILE':'wrong.yml'}), patch.object(stacks.subprocess,'run') as run:
            stacks.compose(inv.origin('backup'),'one','ps')
            self.assertNotIn('COMPOSE_FILE',run.call_args.kwargs['env'])
            self.assertEqual(run.call_args.kwargs['cwd'],self.root/'one')

    def test_rotated_agent_token_takes_effect_without_restart(self):
        from wpops import agent
        token_file=self.root/'state/agent-token.sha256'
        with patch.object(agent,'TOKEN_FILE',token_file):
            old=agent.rotate_token()
            self.assertEqual(token_file.stat().st_mode & 0o777,0o600)
            handler=agent.make_handler(type('A',(),{'node':lambda self:'n'})())
            def auth(tok):
                h=handler.__new__(handler);h.headers={'Authorization':'Bearer '+tok};return h._auth()
            self.assertTrue(auth(old))
            new=agent.rotate_token()
            self.assertFalse(auth(old));self.assertTrue(auth(new))

    def test_abandoned_job_records_do_not_block_pulls_or_restores(self):
        from wpops import jobs
        with patch.object(jobs,'JOBS_DIR',self.root/'state/jobs'):
            live=jobs.Job('backup.pull',{},'test');live.status='running';live.save()
            self.assertEqual([j['id'] for j in jobs.active('backup.pull')],[live.id])
            dead=jobs.Job('restore.apply',{},'test');dead.status='running'
            dead.owner={'pid':live.owner['pid'],'start':'not-this-process'};dead.save()
            legacy=self.root/'state/jobs/20200101-000000-aaaaaa.json'
            legacy.write_text(json.dumps({'id':'20200101-000000-aaaaaa','kind':'restore.apply','status':'running','created':1}))
            self.assertEqual(jobs.active('restore.apply'),[])
            self.assertEqual(sorted(jobs.reap_stale()),sorted([dead.id,'20200101-000000-aaaaaa']))
            self.assertEqual(jobs.get(dead.id)['status'],'failed')
            self.assertEqual(jobs.get(live.id)['status'],'running')

    def test_prune_spares_running_pull_and_ignores_unverified_sets(self):
        import time
        backups=self.root/'backups'
        def make(set_id,pulled,files=True,age=0):
            d=backups/'src'/set_id;d.mkdir(parents=True)
            if files: (d/'backup_a.zip').write_bytes(b'x')
            (d/'manifest.json').write_text(json.dumps({'files':[{'name':'backup_a.zip','size':1}],'pulled_at':pulled}))
            for f in d.iterdir(): os.utime(f,(time.time()-age,)*2)
        make('20260101-0000-good1',1);make('20260102-0000-good2',1)
        make('20260103-0000-unverified',None,age=3600)      # checksum failed this hour
        make('20260104-0000-running',None,files=False)       # download in progress
        make('20250101-0000-junk',None,files=False,age=3*86400)
        with patch.object(backup,'BACKUPS_DIR',backups), patch.object(backup,'node_config',return_value={'backup':{'retain_sets':2}}):
            rows={s['set']:s for s in backup.list_sets()}
            self.assertFalse(rows['20260103-0000-unverified']['complete'])
            self.assertEqual(backup.prune(log=lambda _:None),['src/20250101-0000-junk'])
            self.assertTrue((backups/'src/20260104-0000-running').exists())
            self.assertTrue((backups/'src/20260101-0000-good1').exists())

    def test_agent_rejects_malformed_backup_requests(self):
        from wpops.agent import Agent, ApiError
        agent=Agent.__new__(Agent);agent.inv=self.inventory();agent.cfg={'node':'backup'};agent.reload=lambda:None
        with patch.object(backup,'prune') as prune:
            for body in ({'retain':0},{'retain':-1},{'retain':'2'},{'source':['x']}):
                with self.assertRaises(ApiError) as e: agent.route('POST','/v1/backups/prune',{},body,'t')
                self.assertEqual(e.exception.status,400)
            prune.assert_not_called()
        for path,body in (('/v1/backups/keep',{}),('/v1/backups/delete',{'set':5}),
                          ('/v1/restore/apply',{'set':'a/b','stack':'s','entities':'db'}),
                          ('/v1/restore/apply',{'set':'a/b','stack':'s','sites':[1]})):
            with self.assertRaises(ApiError) as e: agent.route('POST',path,{},body,'t')
            self.assertEqual(e.exception.status,400)
        with self.assertRaises(ApiError) as e: agent.route('GET','/v1/jobs',{'limit':['x']},{},'t')
        self.assertEqual(e.exception.status,400)

    def _fake_cloudflare(self, accounts_env, **flags):
        """Copy the Cloudflare scripts into a scratch project and put a fake curl/docker on PATH."""
        proj=self.root/'cfproj';(proj/'scripts').mkdir(parents=True)
        for f in (ROOT/'scripts').glob('cf-*.sh'): shutil.copy(f,proj/'scripts')
        (proj/'.env').write_text('CF_STANDBY_DOMAIN=standby.example\nCF_CLOUD_IP=192.0.2.10\n'+accounts_env)
        bin_dir=self.root/'fakebin';bin_dir.mkdir()
        log=self.root/'curl.log'
        (bin_dir/'curl').write_text('#!/usr/bin/env python3\n' + r"""
import json, os, re, sys
args = sys.argv[1:]
method, url, headers, body = 'GET', args[-1], [], ''
for i, a in enumerate(args):
    if a == '-X': method = args[i + 1]
    if a == '-H':
        h = args[i + 1]
        headers += open(h[1:]).read().splitlines() if h.startswith('@') else [h]
    if a == '--data-binary' and args[i + 1] == '@-': body = sys.stdin.read()
state = json.load(open(os.environ['FAKE_CF_STATE']))
with open(os.environ['FAKE_CF_LOG'], 'a') as fh:
    fh.write(json.dumps({'method': method, 'url': url, 'argv': args, 'headers': headers, 'body': body}) + '\n')
def out(result, code=200, ok=True):
    print(json.dumps({'success': ok, 'result': result, 'errors': [] if ok else [{'message': 'simulated'}],
                      'result_info': {'total_pages': 1}}))
    print(code)
    sys.exit(0)
path = url.split('/client/v4', 1)[1]
if path.startswith('/zones?'):
    name = re.search(r'name=([^&]+)', path)
    out([{'id': 'zone-' + name.group(1), 'name': name.group(1)}] if name and name.group(1) in state['zones'] else [])
if '/dns_records' in path:
    zone = path.split('/')[2][5:]
    recs = state['records'].get(zone, [])
    if method == 'GET':
        t = re.search(r'type=([A-Z]+)', path)
        out([r for r in recs if not t or r['type'] == t.group(1)])
    if method == 'DELETE':
        rid = path.rsplit('/', 1)[1]
        state['records'][zone] = [r for r in recs if r['id'] != rid]
    if method == 'POST':
        rec = json.loads(body)
        if rec['type'] in state.get('fail_create', []): out(None, 400, False)
        rec['id'] = 'new-' + rec['type']
        state['records'].setdefault(zone, []).append(rec)
    json.dump(state, open(os.environ['FAKE_CF_STATE'], 'w'))
    out({})
if '/cfd_tunnel?' in path: out([{'id': 'tun1', 'name': 'wp-docker-customer'}])
if path.endswith('/configurations'):
    if method == 'PUT':
        state['ingress'] = json.loads(body)['config']['ingress']; json.dump(state, open(os.environ['FAKE_CF_STATE'], 'w'))
    out({'config': {'ingress': state['ingress']}})
out({'status': 'active'})
""")
        (bin_dir/'docker').write_text('#!/bin/sh\necho healthy\n')
        for f in bin_dir.iterdir(): f.chmod(0o755)
        cf_state=self.root/'cf-state.json';cf_state.write_text(json.dumps(flags))
        env={**os.environ,'PATH':f'{bin_dir}:{os.environ["PATH"]}','FAKE_CF_LOG':str(log),'FAKE_CF_STATE':str(cf_state)}
        def calls(): return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return proj,env,calls,lambda: json.loads(cf_state.read_text())

    ACCOUNT_ENV='CF_ACCOUNT_1_NAME=customer\nCF_ACCOUNT_1_ID=acct\nCF_ACCOUNT_1_TOKEN=secret-token-value\nCF_ACCOUNT_1_DOMAINS=good.example,missing.example\n'

    @unittest.skipUnless(shutil.which('jq'), 'jq is needed for the Cloudflare shell scripts')
    def test_shell_backup_survives_missing_zone_and_keeps_token_out_of_argv(self):
        proj,env,calls,_=self._fake_cloudflare(self.ACCOUNT_ENV,zones=['good.example'],records={},ingress=[])
        r=subprocess.run(['bash',str(proj/'scripts/cf-backup.sh')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertTrue(list((proj/'data/cloudflare/backups/dns/good.example').glob('*.json')))
        self.assertTrue(calls())
        for c in calls():
            self.assertNotIn('secret-token-value',' '.join(c['argv']))
            self.assertIn('Authorization: Bearer secret-token-value',c['headers'])

    @unittest.skipUnless(shutil.which('jq'), 'jq is needed for the Cloudflare shell scripts')
    def test_shell_failover_routes_tunnel_before_dns(self):
        rec={'id':'a1','type':'A','name':'good.example','content':'192.0.2.10','proxied':True}
        proj,env,calls,cf=self._fake_cloudflare(self.ACCOUNT_ENV.replace(',missing.example',''),zones=['good.example'],
                                                records={'good.example':[rec]},ingress=[{'service':'http_status:404'}])
        r=subprocess.run(['bash',str(proj/'scripts/cf-failover.sh')],env=env,input='FAILOVER\ny\n',capture_output=True,text=True,timeout=120)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        writes=[(c['method'],c['url'].rsplit('/',1)[1]) for c in calls() if c['method']!='GET']
        self.assertEqual(writes[0],('PUT','configurations'))
        self.assertEqual([w[0] for w in writes[1:]],['DELETE','POST'])
        self.assertEqual(cf()['ingress'][0],{'hostname':'good.example','service':'http://nginx:80'})
        self.assertEqual([(x['type'],x['content']) for x in cf()['records']['good.example']],[('CNAME','tun1.cfargotunnel.com')])

    @unittest.skipUnless(shutil.which('jq'), 'jq is needed for the Cloudflare shell scripts')
    def test_shell_rollback_keeps_tunnel_route_when_dns_restore_fails(self):
        rec={'id':'c1','type':'CNAME','name':'good.example','content':'tun1.cfargotunnel.com','proxied':True}
        ingress=[{'hostname':'good.example','service':'http://nginx:80'},{'service':'http_status:404'}]
        proj,env,calls,cf=self._fake_cloudflare(self.ACCOUNT_ENV.replace(',missing.example',''),zones=['good.example'],
                                                records={'good.example':[rec]},ingress=ingress,fail_create=['A'])
        r=subprocess.run(['bash',str(proj/'scripts/cf-rollback.sh')],env=env,input='ROLLBACK\ny\n',capture_output=True,text=True,timeout=120)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual([(x['type'],x['content']) for x in cf()['records']['good.example']],[('CNAME','tun1.cfargotunnel.com')])
        self.assertEqual(cf()['ingress'],ingress)
        self.assertFalse(any(c['method']=='PUT' for c in calls()))

    def test_webhook_does_not_follow_redirects(self):
        from wpops import notify
        self.assertTrue(any(isinstance(h,notify._NoRedirect) for h in notify._OPENER.handlers))

    def test_nginx_denies_host_alias_map(self):
        conf=(ROOT/'config/nginx/conf.d/default.conf').read_text()
        self.assertRegex(conf,r'location = /wp-content/wpops-host-aliases\.json \{\s*deny all;')

    def test_edge_network_subnet_matches_trusted_connector_ranges(self):
        import ipaddress, re
        conf=(ROOT/'config/nginx/conf.d/connector-real-ip.conf').read_text()
        trusted=[ipaddress.ip_network(c) for c in re.findall(r'^set_real_ip_from (\S+);',conf,re.M)]
        edge=ipaddress.ip_network(stacks.EDGE_SUBNET)
        hosts=set(edge.hosts())
        covered={ip for n in trusted for ip in n if ip in hosts}
        # every connector address except the gateway (.1), nothing outside the edge network
        self.assertEqual(covered,hosts-{next(edge.hosts())})
        self.assertTrue(all(n.subnet_of(edge) for n in trusted))
        for st in range(1,255):   # no stack frontend/backend network overlaps it
            self.assertFalse(ipaddress.ip_network(f'10.214.{st}.0/24').overlaps(edge))
        calls=[]
        def run(cmd,**kw):
            calls.append(cmd);return subprocess.CompletedProcess(cmd,1 if cmd[2]=='inspect' else 0,'','')
        with patch.object(provision.subprocess,'run',side_effect=run):
            self.assertIsNone(provision.ensure_edge())
        self.assertEqual(calls[-1],['docker','network','create','--subnet','10.214.255.0/24','wpops-edge'])
        with patch.object(provision.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'172.20.0.0/16 ','')):
            self.assertIn('172.20.0.0/16',provision.ensure_edge())

    def test_tunnel_backup_container_is_not_on_edge_network(self):
        from unittest.mock import MagicMock
        cf=MagicMock();cf.verify_token.return_value={'status':'active'};cf.tunnel.return_value={'id':'tid'};cf.tunnel_token.return_value='t'
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        with patch.object(tunnels,'Cloudflare',return_value=cf), patch.object(tunnels,'cf_accounts',return_value=accounts),patch.object(tunnels,'load_env',return_value={}):
            tunnels.ensure(start=False,log=lambda _:None)
        doc=json.loads((self.root/'docker-compose.wpops-tunnels.json').read_text())
        self.assertNotIn('networks',doc['services']['cf-backup'])
        self.assertIn('edge',doc['services']['cloudflared-customer']['networks'])

    @unittest.skipUnless(os.environ.get('WPOPS_TEST_NGINX') and shutil.which('unshare') and shutil.which('ip') and shutil.which('curl'),
                         'set WPOPS_TEST_NGINX to an nginx binary (needs unshare/ip/curl for a private network namespace)')
    def test_live_nginx_rate_limits_real_visitor_only_from_connectors(self):
        if subprocess.run(['unshare','-rn','true'],capture_output=True).returncode:
            self.skipTest('no permission for a private network namespace')
        nginx=os.environ['WPOPS_TEST_NGINX'];conf=self.root/'nginx';
        for d in ('conf.d','wpops','wpops-http','logs','tmp','www'): (conf/d).mkdir(parents=True)
        mime=Path(nginx).resolve().parents[2]/'etc/nginx/mime.types'
        (conf/'mime.types').write_text(mime.read_text() if mime.exists() else 'types { text/html html; }\n')
        (conf/'fastcgi_params').write_text('')
        for f in (ROOT/'config/nginx/conf.d').glob('*.conf'):
            text=f.read_text().replace('wordpress:9000','127.0.0.1:9').replace('listen 80;','listen 127.0.0.1:18099;')
            (conf/'conf.d'/f.name).write_text(text.replace('/var/www/html',str(conf/'www')).replace('/etc/nginx',str(conf)))
        main=(ROOT/'config/nginx/nginx.production.conf').read_text().replace('/etc/nginx',str(conf))
        main=main.replace('user  nginx;','user root root;').replace('/var/log/nginx',str(conf/'logs')).replace('/var/run/nginx.pid',str(conf/'nginx.pid'))
        temp=' '.join(f'{k}_temp_path {conf}/tmp/{k};' for k in ('client_body','proxy','fastcgi','uwsgi','scgi'))
        main=main.replace('http {','http {\n    '+temp,1).replace("'$remote_addr ","'$remote_addr|$status ")
        (conf/'nginx.conf').write_text(main)
        script=r"""set -e
ip link set lo up
for a in 10.214.255.5 10.214.255.1 10.214.1.5; do ip addr add $a/32 dev lo; done
"$1" -t -c "$2/nginx.conf" -e "$2/logs/error.log"
"$1" -c "$2/nginx.conf" -e "$2/logs/error.log"
sleep 0.5
for src in 10.214.255.5 10.214.255.1 10.214.1.5; do
  for n in $(seq 1 13); do
    curl -s -o /dev/null --interface $src -H "CF-Connecting-IP: 198.51.100.$n" http://127.0.0.1:18099/wp-login.php
  done
done
"$1" -c "$2/nginx.conf" -e "$2/logs/error.log" -s stop
"""
        r=subprocess.run(['unshare','-rn','bash','-c',script,'t',nginx,str(conf)],capture_output=True,text=True,timeout=60)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        lines=[l.split(' ',1)[0].split('|') for l in (conf/'logs/access.log').read_text().splitlines()]
        connector,gateway,php=lines[:13],lines[13:26],lines[26:]
        # From a connector: each visitor is its own client, nobody is rate limited.
        self.assertEqual([a for a,_ in connector],[f'198.51.100.{n}' for n in range(1,14)])
        self.assertNotIn('429',[s for _,s in connector])
        # From the gateway (loopback port) or a frontend container: the header is ignored.
        for src,rows in (('10.214.255.1',gateway),('10.214.1.5',php)):
            self.assertEqual({a for a,_ in rows},{src})
            self.assertIn('429',[s for _,s in rows])


    # ---------------------------------------------------------- 2026-10-29 end-to-end findings

    def test_init_installs_on_an_empty_database_without_echoing_the_password(self):
        provision.create('one','single','one.example',18081)
        password=json.loads((self.root/'stacks/one/.env').read_text().split('WP_ADMIN_PASSWORD=',1)[1].splitlines()[0])
        calls=[]
        def wp(origin,name,*args,**kw):
            calls.append(args)
            if args[:2]==('core','is-installed'): return subprocess.CompletedProcess(args,1,'','')
            if args[:2]==('db','tables'):   # needs an installed WordPress: fails on an empty database
                return subprocess.CompletedProcess(args,1,'','Error: The site you have requested is not installed.')
            if args[:2]==('db','query'): return subprocess.CompletedProcess(args,0,tables[0],'')
            return subprocess.CompletedProcess(args,0,'','')
        def compose(origin,name,*args,**kw):
            out=f'2/3 [--admin_password=<password>]: {password}\nwp core install --admin_password=\'{password}\'\nSuccess: WordPress installed successfully.' if 'exec' in args else ''
            return subprocess.CompletedProcess(args,0,out,'')
        tables=['']
        with patch.object(provision,'wp',side_effect=wp), patch.object(provision,'compose',side_effect=compose), \
             patch.object(provision,'ensure_edge'), patch.object(provision,'install_runtime'):
            result=provision.initialise('one','admin@example.com')
            self.assertEqual(result['status'],'installed')
            self.assertNotIn(password,json.dumps(result))
            self.assertIn('Success: WordPress installed successfully.',result['output'])
            tables[0]='wp_options\n'
            with self.assertRaisesRegex(stacks.StackError,'not empty'):
                provision.initialise('one','admin@example.com')

    def test_new_stack_refuses_a_loopback_port_in_use(self):
        import socket
        with socket.socket() as busy, patch.object(provision,'port_in_use',side_effect=REAL_PORT_IN_USE):
            busy.bind(('127.0.0.1',0));busy.listen(1);port=busy.getsockname()[1]
            with self.assertRaisesRegex(config.ConfigError,f'port {port} is already in use'):
                provision.create('one','single','one.example',port)
        self.assertFalse((self.root/'stacks/one').exists())

    def test_failover_to_a_tunnel_is_proxied_even_for_dns_only_records(self):
        self.add_stack('one');self.add_site('one','one');self.raw['sites'][0]['domains']=['one.example'];inv=self.inventory()
        from unittest.mock import MagicMock
        cf=MagicMock();cf.zone_id.return_value='zone'
        cf.dns_records.return_value=[{'id':'a','type':'A','content':'192.0.2.1','name':'one.example','ttl':300,'proxied':False},
                                     {'id':'b','type':'AAAA','content':'2001:db8::1','name':'one.example','ttl':300,'proxied':False}]
        cfg={'node':'backup','failover':{'default_to':'backup','tunnel_service':'http://nginx:80'}}
        with patch.object(failover,'Cloudflare',return_value=cf):
            p=failover.plan(inv,inv.sites,cfg=cfg,accounts={'customer':config.CfAccount(1,'customer','acct','secret')})
        self.assertEqual(p.changes[0].create['proxied'],True)
        self.assertEqual([r['proxied'] for r in p.changes[0].original_records],[False,False])

    def test_failed_rollback_create_keeps_the_name_resolving(self):
        self.add_stack('one');self.add_site('one','one');self.raw['sites'][0]['domains']=['one.example'];inv=self.inventory()
        original={'id':'orig','type':'A','content':'192.0.2.1','name':'one.example','ttl':300,'proxied':False}
        tunnel={'id':'new','type':'CNAME','content':'tid.cfargotunnel.com','name':'one.example','ttl':1,'proxied':True}
        state.save_overrides({'one.example':{'site':'one','from':'primary','to':'backup','stack':'one','cf_account':'customer',
                                             'zone_id':'zone','tunnel_id':'tid','original_records':[original],'status':'active'}})
        dns=[dict(tunnel)];fail=[True]
        from unittest.mock import MagicMock
        cf=MagicMock()
        cf.dns_records.side_effect=lambda *a,**k:copy.deepcopy(dns)
        cf.delete_record.side_effect=lambda z,rid:dns.__setitem__(slice(None),[r for r in dns if r['id']!=rid])
        def create(z,r):
            if r['type']=='A' and fail[0]:
                fail[0]=False;raise failover.CfError('simulated create failure')
            dns.append({**r,'id':'x'+r['type']});return dns[-1]
        cf.create_record.side_effect=create
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        with patch.object(failover,'Cloudflare',return_value=cf):
            result=failover.rollback(inv,'test','backup',all_sites=True,accounts=accounts,log=lambda _:None)
            self.assertTrue(result['failed'])
            self.assertEqual([(r['type'],r['content']) for r in dns],[('CNAME','tid.cfargotunnel.com')])
            self.assertEqual(state.overrides()['one.example']['status'],'rollback-failed')
            cf.remove_tunnel_ingress_hosts.assert_not_called()
            result=failover.rollback(inv,'test','backup',all_sites=True,accounts=accounts,log=lambda _:None)
            self.assertEqual(result['failed'],[])
        self.assertEqual([(r['type'],r['content'],r['proxied'],r['ttl']) for r in dns],[('A','192.0.2.1',False,300)])
        self.assertEqual(state.overrides(),{})

    def test_tunnels_keep_an_existing_connector_when_its_account_fails(self):
        from unittest.mock import MagicMock
        cf=MagicMock();cf.verify_token.return_value={'status':'active'};cf.tunnel.return_value={'id':'tid'}
        cf.tunnel_token.side_effect=['token-1',tunnels.CfError('CF GET token -> HTTP 500')]
        accounts={'customer':config.CfAccount(1,'customer','acct','secret')}
        with patch.object(tunnels,'Cloudflare',return_value=cf), patch.object(tunnels,'cf_accounts',return_value=accounts), \
             patch.object(tunnels,'load_env',return_value={}):
            self.assertTrue(tunnels.ensure(start=False,log=lambda _:None)['customer']['ok'])
            self.assertFalse(tunnels.ensure(start=False,log=lambda _:None)['customer']['ok'])
        doc=json.loads((self.root/'docker-compose.wpops-tunnels.json').read_text())
        # The service stays; its token is never written to the file (it is passed at `up`).
        self.assertEqual(doc['services']['cloudflared-customer']['environment']['TUNNEL_TOKEN'],'${TUNNEL_TOKEN_CUSTOMER}')
        self.assertNotIn('token-1',(self.root/'docker-compose.wpops-tunnels.json').read_text())

    def test_cli_reports_cloudflare_errors_and_passes_compose_through(self):
        from wpops import __main__ as cli
        import io
        err=io.StringIO()
        with patch.object(cli,'load_inventory',return_value=self.inventory()), patch.object(cli,'node_config',return_value={'node':'backup'}), \
             patch.object(routes,'plan',side_effect=cli.CfError('CF GET /zones -> HTTP 403: denied')), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['routes','plan']),1)
        self.assertIn('HTTP 403',err.getvalue());self.assertNotIn('Traceback',err.getvalue())
        self.add_stack('one')
        with patch.object(cli,'load_inventory',return_value=self.inventory()), patch.object(cli,'node_config',return_value={'node':'backup'}), \
             patch.object(stacks,'compose',return_value=subprocess.CompletedProcess([],3,None,None)) as compose:
            self.assertEqual(cli.main(['stack','compose','one','exec','-T','nginx','nginx','-t']),3)
        self.assertEqual(compose.call_args.args[2:],('exec','-T','nginx','nginx','-t'))
        self.assertFalse(compose.call_args.kwargs['capture'])
        err=io.StringIO()
        failure=subprocess.CalledProcessError(1,['docker','compose','up'],'','container wps-one-nginx-1 is unhealthy')
        with patch.object(cli,'load_inventory',return_value=self.inventory()), patch.object(cli,'node_config',return_value={'node':'backup'}), \
             patch.object(provision,'ensure_edge'), patch.object(stacks,'compose',side_effect=failure), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['stack','up','one']),1)
        self.assertIn('is unhealthy',err.getvalue())

    def test_cli_schedule_rejects_unknown_backup_sources(self):
        from wpops import __main__ as cli
        import io
        self.raw['backup_sources']={'shop-source':{'origin':'primary','sync_api':'https://shop.example'}}
        err=io.StringIO()
        with patch.object(cli,'load_inventory',return_value=self.inventory()), patch.object(backup,'set_schedule') as schedule, \
             contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['backup','schedule','--sources','shop-source,nope']),1)
            schedule.assert_not_called()
        self.assertIn('nope',err.getvalue())

    def test_agent_validates_restore_requests_before_starting_a_job(self):
        from wpops.agent import Agent, ApiError
        self.add_stack('one');self.add_site('one','one')
        agent=Agent.__new__(Agent);agent.inv=self.inventory();agent.cfg={'node':'backup'};agent.reload=lambda:None
        with patch('wpops.agent.jobs.Job') as job, patch('wpops.agent.jobs.start_background'), patch('wpops.agent.jobs.active',return_value=[]), \
             patch.object(backup,'BACKUPS_DIR',self.root/'backups'):
            for body in ({'set':'../../etc/x','stack':'one','all':True},
                         {'set':'src/20260101-0000-abcd','stack':'../one','all':True},
                         {'set':'src/20260101-0000-abcd','stack':'one','all':True},
                         {'set':'src/20260101-0000-abcd','stack':'one','sites':['nope']}):
                with self.assertRaises(ApiError) as e: agent.route('POST','/v1/restore/apply',{},body,'t')
                self.assertEqual(e.exception.status,400,body)
            job.assert_not_called()
            d=self.root/'backups/src/20260101-0000-abcd';d.mkdir(parents=True);(d/'manifest.json').write_text('{}')
            agent.route('POST','/v1/restore/apply',{},{'set':'src/20260101-0000-abcd','stack':'one','sites':['one']},'t')
            job.assert_called_once()

    def test_backup_pull_completes_and_records_the_source_version(self):
        import hashlib
        payload=b'database dump'
        name='backup_2026-10-29-0342_site_ABCD1234-db.gz'
        listing={'mode':'single','site_url':'https://shop.example','sites':[{'blog_id':1,'domain':'shop.example'}],
                 'sets':[{'nonce':'ABCD1234','timestamp':1793245320,'label':'t','full':True,
                          'files':[{'name':name,'size':len(payload)}]}]}
        class Client:
            deleted=[]
            def ping(self): return {'version':'1.6.3','site_url':'https://shop.example','mode':'single'}
            def start_backup(self,label,entities=None,db=True): return {'nonce':'ABCD1234'}
            def job(self,nonce): return {'status':'complete','terminal':True,'succeeded':True}
            def sets(self): return copy.deepcopy(listing)
            def download(self,nonce,fname,dest,size,log): dest.write_bytes(payload)
            def hashes(self,nonce,log): return {name:hashlib.sha256(payload).hexdigest()}
            def delete_remote(self,nonce): Client.deleted.append(nonce)
        self.raw['backup_sources']={'shop-source':{'origin':'primary','sync_api':'https://shop.example'}}
        inv=self.inventory()
        job=type('Job',(),{'log':staticmethod(lambda _:None),'set_progress':lambda *a,**k:None,'progress':{},'id':'j','actor':'t'})()
        cfg={'backup':{'retain_sets':4,'entities':[],'delete_on_source':True,'max_set_gb':1,'pull_timeout_min':1}}
        with patch.object(backup,'client_for',return_value=Client()), patch.object(backup,'node_config',return_value=cfg), \
             patch.object(backup,'BACKUPS_DIR',self.root/'backups'), patch.object(backup.disk,'require'), \
             patch.object(backup.state,'locked',return_value=contextlib.nullcontext()):
            result=backup.pull(job,inv,'shop-source')
            _,man=backup.manifest(result['set'])
        self.assertEqual(man['plugin_version'],'1.6.3')
        self.assertTrue(man['pulled_at'])
        self.assertEqual(Client.deleted,['ABCD1234'])

    def test_setup_notes_sunrise_only_for_unmanaged_multisite_stacks(self):
        node=self.root/'node';(node/'config').mkdir(parents=True);(node/'plugin').symlink_to(ROOT/'plugin')
        (node/'config/wpops.json').write_text(json.dumps({'node':'n'}))
        stacks_cfg={}
        for name,managed in (('managed',True),('legacy',False)):
            (node/name/'data/wordpress/wp-content').mkdir(parents=True)
            stacks_cfg[name]={'path':str(node/name),'mode':'multisite','managed':managed}
        (node/'legacy/.env').write_text('WP_SUNRISE=false\n')
        (node/'config/inventory.json').write_text(json.dumps({'origins':{'n':{'kind':'node','stacks':stacks_cfg,'tunnels':{}}},'sites':[]}))
        r=subprocess.run([str(ROOT/'scripts/wpops/bin/wpops'),'setup'],env={**os.environ,'WPOPS_REPO_DIR':str(node)},
                         capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        notes=[l for l in r.stdout.splitlines() if 'WP_SUNRISE' in l]
        self.assertEqual(len(notes),1);self.assertIn(str(node/'legacy'),notes[0])

    def test_cf_api_url_points_both_tools_at_a_test_double(self):
        env={**os.environ,'CF_API_URL':'http://127.0.0.1:8999/client/v4'}
        r=subprocess.run([sys.executable,'-c','import sys;sys.path.insert(0,sys.argv[1]);from wpops import config;print(config.CF_API)',
                          str(ROOT/'scripts/wpops')],env=env,capture_output=True,text=True)
        self.assertEqual(r.stdout.strip(),'http://127.0.0.1:8999/client/v4',r.stderr)
        r=subprocess.run(['bash','-c','source "$1/scripts/cf-common.sh"; echo "$CF_API"','t',str(ROOT)],env=env,capture_output=True,text=True)
        self.assertEqual(r.stdout.strip(),'http://127.0.0.1:8999/client/v4',r.stderr)
        env.pop('CF_API_URL')
        r=subprocess.run(['bash','-c','source "$1/scripts/cf-common.sh"; echo "$CF_API"','t',str(ROOT)],env=env,capture_output=True,text=True)
        self.assertEqual(r.stdout.strip(),'https://api.cloudflare.com/client/v4')

    @unittest.skipUnless(shutil.which('docker'), 'needs the Docker CLI with Compose v2 (no daemon)')
    def test_compose_capabilities_and_legacy_cli_network(self):
        r=subprocess.run(['docker','compose','-f',str(ROOT/'docker-compose.yml'),'--env-file',str(ROOT/'.env.example'),
                          'config','--format','json'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        legacy=json.loads(r.stdout)['services']
        self.assertTrue({'CHOWN','FOWNER'} <= set(legacy['mariadb']['cap_add']))   # fresh ./data/mysql
        self.assertIn('CHOWN',legacy['redis']['cap_add'])                           # fresh ./data/redis
        self.assertIn('KILL',legacy['wordpress']['cap_add'])                        # php-fpm reload/stop
        self.assertIn('frontend',legacy['wp-cron']['networks'])                     # not only the internal network
        result=provision.create('one','single','one.example',18081)
        env={k:v for k,v in os.environ.items() if not k.startswith('COMPOSE_') and k!='WPOPS_REPO_DIR'}
        r=subprocess.run(['docker','compose','config','--format','json'],cwd=result['path'],env=env,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('KILL',json.loads(r.stdout)['services']['wordpress']['cap_add'])

    @unittest.skipUnless(shutil.which('docker'), 'needs the Docker CLI with Compose v2 (no daemon)')
    def test_env_values_stay_out_of_php_source_and_redis_command_line(self):
        tricky="x'); system('id'); //"
        pw='redis-"pass#word\\x'
        env={**os.environ,'WP_DOMAIN_CURRENT_SITE':tricky,'REDIS_HOST':tricky,'REDIS_PASSWORD':pw}
        r=subprocess.run(['docker','compose','-f',str(ROOT/'docker-compose.yml'),'--env-file',str(ROOT/'.env.example'),
                          'config','--format','json'],env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        doc=json.loads(r.stdout)['services']
        for svc in ('wordpress','wp-cron'):
            e=doc[svc]['environment']
            self.assertNotIn(tricky,e['WORDPRESS_CONFIG_EXTRA'])
            self.assertNotIn('${',e['WORDPRESS_CONFIG_EXTRA'])
            self.assertEqual(e['WP_DOMAIN_CURRENT_SITE'],tricky)
            self.assertEqual(e['REDIS_PASSWORD'],pw)
        redis=doc['redis']
        visible=json.dumps([redis.get('command'),redis.get('entrypoint'),redis.get('healthcheck')])
        self.assertNotIn('pass#word',visible)
        self.assertEqual(redis['environment']['REDIS_PASSWORD'],pw)
        self.assertNotIn('pass#word',json.dumps(doc['cf-backup']))
        self.assertFalse(any('.env' in str(v) for v in doc['cf-backup']['volumes']))
        self.assertTrue(doc['cf-backup']['read_only'])
        # The managed stack template.
        result=provision.create('one','single','one.example',18081)
        stack_pw=config._parse_env(Path(result['path'])/'.env')['REDIS_PASSWORD']
        senv={k:v for k,v in os.environ.items() if not k.startswith('COMPOSE_') and k!='WPOPS_REPO_DIR'}
        r=subprocess.run(['docker','compose','config','--format','json'],cwd=result['path'],env=senv,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
        redis=json.loads(r.stdout)['services']['redis']
        self.assertNotIn(stack_pw,json.dumps([redis.get('command'),redis.get('entrypoint'),redis.get('healthcheck')]))
        self.assertEqual(redis['environment']['REDIS_PASSWORD'],stack_pw)
        self.assertIn('redis-server -',redis['entrypoint'][-1])

    def _php(self, code):
        php=os.environ.get('WPOPS_TEST_PHP') or shutil.which('php')
        script=self.root/'t.php';script.write_text('<?php\n'+code)
        r=subprocess.run([php,'-n',str(script)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(os.environ.get('WPOPS_TEST_PHP') or shutil.which('php'), 'PHP CLI is optional for the PHP checks')
    def test_php_new_subdomain_blogs_follow_the_main_site_scheme(self):
        out=self._php("""define('ABSPATH','/');
$GLOBALS['hooks']=[]; $GLOBALS['opts']=[2=>['home'=>'http://news.net.example','siteurl'=>'http://news.net.example']]; $GLOBALS['blog']=1;
function add_filter(...$a) {} function add_action($h,$cb,$p=10,$n=1) { $GLOBALS['hooks'][$h]=$cb; }
function is_subdomain_install() { return $GLOBALS['subdomain']; } function get_main_site_id($n=null) { return 1; }
function get_home_url($id) { return 'https://net.example'; } function wp_parse_url($u,$c=-1) { return parse_url($u,$c); }
function switch_to_blog($id) { $GLOBALS['blog']=$id; } function restore_current_blog() { $GLOBALS['blog']=1; }
function get_option($o) { return $GLOBALS['opts'][$GLOBALS['blog']][$o]; }
function update_option($o,$v) { $GLOBALS['opts'][$GLOBALS['blog']][$o]=$v; }
require '"""+str(ROOT/'plugin/wpops-policy.php')+"""';
$GLOBALS['subdomain']=false; $GLOBALS['hooks']['wp_initialize_site']((object)['blog_id'=>2,'network_id'=>1]); $path=$GLOBALS['opts'][2];
$GLOBALS['subdomain']=true; $GLOBALS['hooks']['wp_initialize_site']((object)['blog_id'=>2,'network_id'=>1]);
echo json_encode([$path,$GLOBALS['opts'][2],$GLOBALS['blog']]);""")
        self.assertEqual(out[0],{'home':'http://news.net.example','siteurl':'http://news.net.example'})
        self.assertEqual(out[1],{'home':'https://news.net.example','siteurl':'https://news.net.example'})
        self.assertEqual(out[2],1)

    @unittest.skipUnless(os.environ.get('WPOPS_TEST_PHP') or shutil.which('php'), 'PHP CLI is optional for the PHP checks')
    def test_php_alias_of_a_path_based_network_serves_every_blog_below_it(self):
        content=self.root/'wp-content';content.mkdir()
        (content/'wpops-host-aliases.json').write_text(json.dumps({'aliases':{
            'example.org':{'blog_id':1,'host':'paths.example','site':'org'},
            'news.example':{'blog_id':2,'host':'news.net.example','site':'news'}}}))
        sunrise=ROOT/'plugin/kc-host-alias/sunrise.php';alias=ROOT/'plugin/kc-host-alias/kc-host-alias.php'
        out=self._php("""define('ABSPATH','/'); define('WP_CONTENT_DIR','"""+str(content)+"""');
class DB { public $blogs='wp_blogs'; public $site='wp_site'; public $rows;
  function __construct() { $this->rows=[
    (object)['blog_id'=>1,'site_id'=>1,'domain'=>'paths.example','path'=>'/','public'=>1],
    (object)['blog_id'=>2,'site_id'=>1,'domain'=>'paths.example','path'=>'/alpha/','public'=>1],
    (object)['blog_id'=>3,'site_id'=>1,'domain'=>'paths.example','path'=>'/beta/','public'=>1],
    (object)['blog_id'=>4,'site_id'=>2,'domain'=>'news.net.example','path'=>'/','public'=>1]]; }
  function prepare($q,...$a) { return [$q,$a]; }
  function get_row($p) { [$q,$a]=$p; if (strpos($q,'wp_site')!==false) return (object)['id'=>$a[0],'domain'=>'net','path'=>'/','blog_id'=>1];
    foreach ($this->rows as $r) if ($r->blog_id==$a[0]) return $r; return null; }
  function get_results($p) { [$q,$a]=$p; return array_values(array_filter($this->rows,fn($r)=>$r->domain===$a[0]&&$r->site_id==$a[1])); }
  function set_blog_id($b,$s) {} }
$out=[];
foreach ([['example.org','/'],['example.org','/alpha/'],['example.org','/alpha/some-post/?x=1'],['example.org','/beta'],
          ['example.org','/alphabet/'],['news.example','/alpha/']] as [$h,$u]) {
  $GLOBALS['wpdb']=new DB(); $GLOBALS['current_blog']=null; $_SERVER['HTTP_HOST']=$h; $_SERVER['REQUEST_URI']=$u;
  (function() { require '"""+str(sunrise)+"""'; })();
  $out[]=[$h.$u,$GLOBALS['current_blog']->blog_id ?? null,$GLOBALS['path'] ?? null];
}
// The alias plugin keeps the selected blog's path in home/siteurl.
$GLOBALS['filters']=[]; $GLOBALS['cur']=2;
function add_filter($h,$cb,$p=10,$n=1) { $GLOBALS['filters'][$h]=$cb; } function add_action(...$a) {}
function is_multisite() { return true; } function get_current_blog_id() { return $GLOBALS['cur']; }
$GLOBALS['current_blog']=(object)['blog_id'=>2,'path'=>'/alpha/']; $_SERVER['HTTP_HOST']='example.org';
require '"""+str(alias)+"""';
$home=$GLOBALS['filters']['pre_option_home'](false); $GLOBALS['cur']=3;
$other=[$GLOBALS['filters']['pre_option_home'](false), $GLOBALS['filters']['option_home']('https://paths.example/beta')];
echo json_encode([$out,$home,$other]);""")
        blogs,home,other=out
        self.assertEqual([b[1] for b in blogs],[1,2,2,3,1,2])
        self.assertEqual(home,'https://example.org/alpha')
        self.assertEqual(other,[False,'https://example.org/beta'])


if __name__=='__main__': unittest.main()
