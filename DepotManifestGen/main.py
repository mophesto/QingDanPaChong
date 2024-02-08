import vdf
import re
import json
import gevent
import struct
import os.path
import logging
import argparse
import traceback
from pathlib import Path
from binascii import crc32
from gevent.pool import Pool as GPool
from steam.utils.web import make_requests_session
from steam.core.cm import CMClient
from steam.client import SteamClient
from six import itervalues, iteritems
from multiprocessing.dummy import Lock
from steam.enums import EResult, EOSType, EPersonaState
from steam.client.cdn import CDNClient
from steam.enums import EResult, EType
from steam.exceptions import SteamError,ManifestError
from steam.protobufs.content_manifest_pb2 import ContentManifestSignature
from steam.core.crypto import symmetric_decrypt, symmetric_decrypt_ecb

parser = argparse.ArgumentParser()
parser.add_argument('-u', '--username', required=True)
parser.add_argument('-p', '--password', required=False, default='')
parser.add_argument('-a', '--app-id', required=False)
parser.add_argument('-l', '--list-apps', action='store_true', required=False)
parser.add_argument('-s', '--sentry-path', '--ssfn', required=False)
parser.add_argument('-k', '--login-key', required=False)
parser.add_argument('-f', '--two-factor-code', required=False)
parser.add_argument('-A', '--auth-code', required=False)
parser.add_argument('-i', '--login-id', required=False)
parser.add_argument('-c', '--cli', action='store_true', required=False)
parser.add_argument('-L', '--level', required=False, default='INFO')
parser.add_argument('-C', '--credential-location', required=False)
parser.add_argument('-r', '--remove-old', action='store_true', required=False)
parser.add_argument('-n', '--retry', type=int, required=False, default=1)

app_lock ={}
lock = Lock()

class BillingType:
    NoCost = 0
    BillOnceOnly = 1
    BillMonthly = 2
    ProofOfPrepurchaseOnly = 3
    GuestPass = 4
    HardwarePromo = 5
    Gift = 6
    AutoGrant = 7
    OEMTicket = 8
    RecurringOption = 9
    BillOnceOrCDKey = 10
    Repurchaseable = 11
    FreeOnDemand = 12
    Rental = 13
    CommercialLicense = 14
    FreeCommercialLicense = 15
    NumBillingTypes = 16
    PaidList = [BillOnceOnly, BillMonthly, BillOnceOrCDKey, Repurchaseable, Rental, ProofOfPrepurchaseOnly, Gift]


class Result(dict):
    def __init__(self, result=False, code=EResult.Fail, *args, **kwargs):
        super().__init__()
        self.result = result
        self.args = args
        self.code = code
        self.update(kwargs)

    def __bool__(self):
        return bool(self.result)


def decrypt_manifest_gid_2(encrypted_gid, password):
    return struct.unpack('<Q', symmetric_decrypt_ecb(encrypted_gid, password))[0]
    
def get_manifest(cdn, app_id,appinfo,package,manifest, remove_old=False, save_path=None, retry_num=10):
    depot_id = str(manifest.depot_id)
    manifest_gid = str(manifest.gid)
    if not save_path:
        save_path = Path().absolute()
    app_path = save_path / f'depots/{app_id}'
    manifest_path = app_path / f'{depot_id}_{manifest_gid}.manifest'
    if manifest_path.exists():
        return Result(result=True, code=EResult.OK, app_id=app_id, depot_id=depot_id, manifest_gid=manifest_gid)
    while True:
        try:
            depot_key = cdn.get_depot_key(manifest.app_id, manifest.depot_id)
            break
        except KeyboardInterrupt:
            exit(-1)
        except SteamError as e:
            if retry_num == 0:
                return Result(result=False, code=e.eresult, app_id=app_id, depot_id=depot_id, manifest_gid=manifest_gid)
            retry_num -= 1
            log.warning(f'{e} result: {str(e.eresult)}')
            if e.eresult == EResult.AccessDenied or e.eresult == EResult.Fail:
                return Result(result=False, code=e.eresult, app_id=app_id, depot_id=depot_id, manifest_gid=manifest_gid)
            gevent.idle()
        except:
            log.error(traceback.format_exc())
            return Result(result=False, code=EResult.Fail, app_id=app_id, depot_id=depot_id, manifest_gid=manifest_gid)
    log.info(
        f'{"":<10}app_id: {app_id:<8}{"":<10}depot_id: {depot_id:<8}{"":<10}manifest_gid: {manifest_gid:20}{"":<10}DecryptionKey: {depot_key.hex()}')
    manifest.decrypt_filenames(depot_key)
    manifest.signature = ContentManifestSignature()
    for mapping in manifest.payload.mappings:
        mapping.filename = mapping.filename.rstrip('\x00 \n\t')
        mapping.chunks.sort(key=lambda x: x.sha)
    manifest.payload.mappings.sort(key=lambda x: x.filename.lower())
    if not os.path.exists(app_path):
        os.makedirs(app_path)
    depotint = int(depot_id)
    if os.path.isfile(app_path / 'config.json'):
        with open(app_path / 'config.json') as f:
            config = json.load(f)
            config['dlcs'] = package['dlcs']
            config['packagedlcs'] = package['packagedlcs']
            if not depotint in config['depots']:
                config['depots'].append(depotint)
    else:
        #添加配置文件config.json
        json_str = f'''
            {{
            "appId": {app_id},
            "depots": [{depotint}],
            "dlcs": [],
            "packagedlcs": []
            }}'''
        config = json.loads(json_str)
        config['dlcs'] = package['dlcs']
        config['packagedlcs'] = package['packagedlcs']
    #if not os.path.isfile(app_path / 'appinfo.vdf'):
    with open(app_path / 'appinfo.vdf', 'w', encoding='utf-8') as f:
        vdf.dump(appinfo, f, pretty=True)
    if os.path.isfile(app_path / 'config.vdf'):
        with open(app_path / 'config.vdf') as f:
            d = vdf.load(f)
    else:
        d = vdf.VDFDict({'depots': {}})
    d['depots'][depot_id] = {'DecryptionKey': depot_key.hex()}
    d = {'depots': dict(sorted(d['depots'].items()))}
    delete_list = []
    if remove_old:
        for file in app_path.iterdir():
            if file.suffix == '.manifest':
                depot_id_, manifest_gid_ = file.stem.split('_')
                if depot_id_ == str(depot_id) and manifest_gid_ != str(manifest_gid):
                    file.unlink(missing_ok=True)
                    delete_list.append(file.name)
    buffer = manifest.payload.SerializeToString()

    manifest.metadata.crc_clear = crc32(struct.pack('<I', len(buffer)) + buffer)
    with open(manifest_path, 'wb') as f:
        f.write(manifest.serialize(compress=False))
    with open(app_path / 'config.vdf', 'w') as f:
        vdf.dump(d, f, pretty=True)
    with open(app_path / 'config.json', 'w') as f:
        json.dump(config, f)
    return Result(result=True, code=EResult.OK, app_id=app_id, depot_id=depot_id, manifest_gid=manifest_gid,
                  delete_list=delete_list)


class MySteamClient(SteamClient):
    credential_location = str(Path('client').absolute())
    _LOG = logging.getLogger('MySteamClient')
    sentry_path = None
    login_key_path = None

    def __init__(self, credential_location=None, sentry_path=None, retry=1):
        self.retry = retry
        if credential_location:
            self.credential_location = credential_location
        if not Path(self.credential_location).exists():
            Path(self.credential_location).mkdir(parents=True, exist_ok=True)
        if sentry_path:
            if Path(sentry_path).exists():
                self.sentry_path = sentry_path
            elif (Path('client') / sentry_path).exists():
                self.sentry_path = str(Path('client') / sentry_path)
        SteamClient.__init__(self)

    def _handle_update_machine_auth(self, message):
        SteamClient._handle_update_machine_auth(self, message)

    def _handle_login_key(self, message):
        SteamClient._handle_login_key(self, message)
        with (Path(self.credential_location) / f'{self.username}.key').open('w') as f:
            f.write(self.login_key)

    def _handle_logon(self, msg):
        SteamClient._handle_logon(self, msg)

    def _get_sentry_path(self, username):
        if self.sentry_path:
            return self.sentry_path
        else:
            return SteamClient._get_sentry_path(self, username)

    def relogin(self):
        result = SteamClient.relogin(self)
        if result == EResult.InvalidPassword and self.login_key_path:
            self.login_key_path.unlink(missing_ok=True)
        return result   
    
    def __setattr__(self, key, value):
        SteamClient.__setattr__(self, key, value)
        if key == 'username':
            if not self.login_key_path:
                self.login_key_path = Path(self.credential_location) / f'{self.username}.key'
                if not self.login_key and self.login_key_path.exists():
                    with self.login_key_path.open() as f:
                        self.login_key = f.read()

    def connect(self, *args, **kwargs):
        """Attempt to establish connection, see :meth:`.CMClient.connect`"""
        self._bootstrap_cm_list_from_file()
        kwargs['retry'] = self.retry
        return CMClient.connect(self, *args, **kwargs)


class MyCDNClient(CDNClient):
    _LOG = logging.getLogger('MyCDNClient')
    def __init__(self, client,tags,repo):
        """CDNClient allows loading and reading of manifests for Steam apps are used
        to list and download content

        :param client: logged in SteamClient instance
        :type  client: :class:`.SteamClient`
        """
        self.gpool = GPool(8)            #: task pool
        self.steam = client    #: SteamClient instance
        self.tags = tags
        self.repo = repo
        if self.steam:
            self.cell_id = self.steam.cell_id

        self.web = make_requests_session()
        self.depot_keys = {}             #: depot decryption keys
        self.manifests = {}              #: CDNDepotManifest instances
        self.app_depots = {}             #: app depot info
        self.beta_passwords = {}         #: beta branch decryption keys
        self.licensed_app_ids = set()    #: app_ids that the SteamClient instance has access to
        self.licensed_depot_ids = set()  #: depot_ids that the SteamClient instance has access to

        if not self.servers:
            self.fetch_content_servers()

        #self.load_licenses()
            
    def load_licenses(self):
        """Read licenses from SteamClient instance, required for determining accessible content"""
        self.licensed_app_ids.clear()
        self.licensed_depot_ids.clear()
        app_id_list = []
        if self.steam.steam_id.type == EType.AnonUser:
            packages = [17906]
        else:
            if not self.steam.licenses:
                self._LOG.debug("No steam licenses found on SteamClient instance")
                return app_id_list

            packages = list(map(lambda l: {'packageid': l.package_id, 'access_token': l.access_token},
                                itervalues(self.steam.licenses)))
        #改在初始化时获取app_id_list
        for package_id, info in iteritems(self.steam.get_product_info(packages=packages,timeout=30)['packages']):
            if 'depotids' in info and info['depotids'] and info['billingtype'] in BillingType.PaidList:
                app_id_list.extend(list(info['appids'].values()))
            self.licensed_app_ids.update(info['appids'].values())
            self.licensed_depot_ids.update(info['depotids'].values())
        return app_id_list

    def get_app_depot_info(self, app_id):
        if app_id not in self.app_depots:
            self.app_depots[app_id] = self.steam.get_product_info([app_id],timeout=30)['apps'][app_id]
        return self.app_depots[app_id]

    def check_manifest_exist(self, depot_id, manifest_gid):
        for tag in set([i.name for i in self.repo.tags] + [*self.tags.get(depot_id,set())]):
            if f'{depot_id}_{manifest_gid}' == tag:
                return True
        return False
        
    def get_manifest_request_code(self, app_id, depot_id, manifest_gid, branch='public', branch_password_hash=None):
        body = {
            "app_id":      int(app_id),
            "depot_id":    int(depot_id),
            "manifest_id": int(manifest_gid),
        }

        if branch and branch.lower() != 'public':
            body['app_branch'] = branch

            if branch_password_hash:
                body['branch_password_hash'] = branch_password_hash

        resp = self.steam.send_um_and_wait(
            'ContentServerDirectory.GetManifestRequestCode#1',
            body,
            timeout=30,
        )

        if resp is None or resp.header.eresult != EResult.OK:
                raise SteamError("Failed to get manifest code for %s, %s, %s" % (app_id, depot_id, manifest_gid),
                                 EResult.Timeout if resp is None else EResult(resp.header.eresult))

        return resp.body.manifest_request_code
        
    def get_manifests(self, app_id,app, branch='public', password=None, filter_func=None, decrypt=False):
        #depots = self.get_app_depot_info(app_id)
        depots = app.get('depots',{})
        rets = {'manifests':[],'depots':[]}
        if not depots:
            return rets
        global app_lock
        app_lock.setdefault(str(app_id), {})
        is_enc_branch = False
        if branch in depots.get('branches', {}):
           if int(depots['branches'][branch].get('pwdrequired', 0)) > 0:
               is_enc_branch = True
               if (app_id, branch) not in self.beta_passwords:
                   if not password:
                       raise SteamError("Branch %r requires a password" % branch)

                   result = self.check_beta_password(app_id, password)

                   if result != EResult.OK:
                       raise SteamError("Branch password is not valid. %r" % result)

                   if (app_id, branch) not in self.beta_passwords:
                       raise SteamError("Incorrect password for branch %r" % branch)

        def async_fetch_manifest(
            app_id, depot_id, manifest_gid, decrypt, depot_name, branch_name, branch_pass
        ):
            try:
                manifest_code = self.get_manifest_request_code(
                    app_id, depot_id, int(manifest_gid), branch_name, branch_pass
                )
            except SteamError as exc:
                return ManifestError("Failed to acquire manifest code", app_id, depot_id, manifest_gid, exc)

            try:
                manifest = self.get_manifest(
                    app_id, depot_id, int(manifest_gid), decrypt=decrypt, manifest_request_code=manifest_code
                )
            except Exception as exc:
                return ManifestError("Failed download", app_id, depot_id, manifest_gid, exc)
            app_lock[str(app_id)][str(depot_id)]= True
            manifest.name = depot_name
            return manifest
                    
        tasks = []
        shared_depots = {}

        for depot_id, depot_info in iteritems(depots):
            if not depot_id.isdigit():
                continue

            depot_id = int(depot_id)

            # if filter_func set, use it to filter the list the depots
            if filter_func and not filter_func(depot_id, depot_info):
                continue

            # if we have no license for the depot, no point trying as we won't get depot_key
            if not self.has_license_for_depot(depot_id):
                self._LOG.debug("No license for depot %s (%s). Skipped",
                                repr(depot_info.get('name', depot_id)),
                                depot_id,
                                )
                continue

            # accumulate the shared depots
            
            if 'depotfromapp' in depot_info:
                shared_depots.setdefault(int(re.search(r'\d+', depot_info['depotfromapp']).group()), set()).add(depot_id)
                continue


            # process depot, and get manifest for branch
            if is_enc_branch:
                egid = depot_info.get('encryptedmanifests', {}).get(branch, {}).get('encrypted_gid_2')

                if egid is not None:
                    manifest_gid = decrypt_manifest_gid_2(unhexlify(egid),
                                                          self.beta_passwords[(app_id, branch)])
                else:
                    manifest_gid = depot_info.get('manifests', {}).get('public',{}).get('gid')
            else:
                manifest_gid = depot_info.get('manifests', {}).get(branch,{}).get('gid')
            if manifest_gid is not None:
                with lock:
                    if not app_lock[str(app_id)].get(str(depot_id)):
                        
                        if self.check_manifest_exist(str(depot_id), manifest_gid):
                            log.info(f'Already got the manifest: {depot_id}_{manifest_gid}')
                            app_lock[str(app_id)][str(depot_id)]= True
                            continue
                        tasks.append(
                            self.gpool.spawn(
                                async_fetch_manifest,
                                app_id,
                                depot_id,
                                manifest_gid,
                                decrypt,
                                depot_info.get('name', depot_id),
                                branch_name=branch,
                                branch_pass=None, # TODO: figure out how to pass this correctly
                            )
                        )
              

        # collect results
        

        for task in tasks:
            result = task.get()
            if isinstance(result, ManifestError):
                raise result
            rets['manifests'].append(result)
        for depot in app_lock[str(app_id)]:
            rets['depots'].append(depot)
                

        # load shared depot manifests
        for app_id, depot_ids in iteritems(shared_depots):
            def nested_ffunc(depot_id, depot_info, depot_ids=depot_ids, ffunc=filter_func):
                return (int(depot_id) in depot_ids
                        and (ffunc is None or ffunc(depot_id,  depot_info)))
            mfs = self.get_manifests(app_id,self.get_app_depot_info(app_id), filter_func=nested_ffunc)
            rets['manifests'] += mfs['manifests']
            rets['depots'] += mfs['depots']
        return rets
log = logging.getLogger('DepotManifestGen')


def main(args=None):
    if args:
        args = parser.parse_args(args)
    else:
        args = parser.parse_args()
    if args.level:
        level = logging.getLevelName(args.level.upper())
    else:
        level = logging.INFO
    logging.basicConfig(format='%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s', level=level)
    steam = MySteamClient(args.credential_location, args.sentry_path, args.retry)
    steam.username = args.username
    if args.login_key:
        steam.login_key = args.login_key
    result = steam.relogin()
    log.error(f'{args.credential_location} {args.sentry_path} {args.retry}')
    if result != EResult.OK:
        if args.cli:
            result = steam.cli_login(args.username, args.password)
        else:
            result = SteamClient.login(args.username, args.password, steam.login_key, args.auth_code, args.two_factor_code,
                                 int(args.login_id) if args.login_id else None) 
    if result != EResult.OK:
        log.error(f'Login failure reason: {result.__repr__()}')
        exit(result)
    app_id_list = []
    app_id_list_all = set()
    depot_id_list = []
    packages_info = []
    cdn = MyCDNClient(steam)
    if cdn.packages_info:
        for package_id, info in steam.get_product_info(packages=cdn.packages_info)['packages'].items():
            if 'appids' in info and 'depotids' in info and info['billingtype'] in BillingType.PaidList:
                app_id_list_all.update(list(info['appids'].values()))
                app_id_list.extend(list(info['appids'].values()))
                depot_id_list.extend(list(info['depotids'].values()))
                packages_info.append((list(info['appids'].values()), list(info['depotids'].values())))
    if args.app_id:
        app_id_list = {int(app_id) for app_id in args.app_id.split(',')}
        app_id_list_all.update(app_id_list)
    fresh_resp = steam.get_product_info(app_id_list)
    app_types = ['game', 'dlc', 'application', 'music']
    if args.list_apps:
        for app_id in app_id_list_all:
            app = fresh_resp['apps'][app_id]
            if 'common' in app and app['common']['type'].lower() in app_types:
                log.info("%s | %s | %s", app_id, app['common']['type'].upper(), app['common']['name'])
        exit()
    result_list = []
    for app_id in app_id_list:
        app = fresh_resp['apps'][app_id]
        if 'common' in app and app['common']['type'].lower() in app_types:
            if 'depots' not in fresh_resp['apps'][app_id]:
                continue
            for depot_id, depot in fresh_resp['apps'][app_id]['depots'].items():
                if 'manifests' in depot and 'public' in depot['manifests'] and int(
                        depot_id) in {*cdn.licensed_depot_ids, *cdn.licensed_app_ids}:
                    manifest_gid = depot['manifests']['public']
                    if isinstance(manifest_gid, dict):
                        manifest_gid = manifest_gid.get('gid')
                    if not isinstance(manifest_gid, str):
                        continue
                    result_list.append(gevent.spawn(get_manifest, cdn, app_id, depot_id,app,manifest_gid, args.remove_old))
                    gevent.idle()
    try:
        gevent.joinall(result_list)
    except KeyboardInterrupt:
        exit(-1)


if __name__ == '__main__':
    main()
