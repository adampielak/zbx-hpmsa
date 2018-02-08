#!/usr/bin/env python3

import os
import requests
import json
import urllib3
import sqlite3
from xml.etree import ElementTree as eTree
from datetime import datetime, timedelta
from hashlib import md5
from argparse import ArgumentParser
from socket import gethostbyname


def hash_pwd(cred, isfile=False):
    """
    The function makes md5 hash of login string.
    :param cred:
    str() Login string in 'user_password' format of path to file.
    :param isfile:
    bool Is string the file path.
    :return:
    str() md5 hash
    """

    if isfile:
        if os.path.exists(cred):
            with open(cred, 'r') as login_file:
                login_data = login_file.readline().replace('\n', '').strip()
                if login_data.find('_') != -1:
                    hashed = md5(login_data.encode()).hexdigest()
                else:
                    hashed = login_data
        else:
            raise SystemExit("ERROR: File password doesn't exists: {}".format(cred))
    else:
        hashed = md5(cred.encode()).hexdigest()
    return hashed


def sql_op(query, fetch_all=False):
    """
    The function works with SQL backend.
    :param query:
    SQL query to execute.
    :param fetch_all:
    Set it True to execute fetchall().
    :return:
    None
    """

    # Determine the path to store cache db
    if os.name == 'posix':
        tmp_dir = '/var/tmp/zbx-hpmsa/'
        # Create temp dir if it's not exists
        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir)
            # Making temp dir writable for zabbix user and group
            os.chmod(tmp_dir, 0o770)
        elif not os.access(tmp_dir, 2):  # 2 - os.W_OK:
            raise SystemExit("ERROR: '{tmp}' not writable for user '{user}'.".format(tmp=tmp_dir,
                                                                                     user=os.getenv('USER')))
    else:
        # Current dir. Yeap, it's easier than getcwd() or os.path.dirname(os.path.abspath(__file__)).
        tmp_dir = ''

    # Cache file name
    cache_db = tmp_dir + 'zbx-hpmsa.cache.db'

    if not any(verb.lower() in query.lower() for verb in ['DROP', 'DELETE', 'TRUNCATE', 'ALTER']):
        conn = sqlite3.connect(cache_db)
        cursor = conn.cursor()
        try:
            if not fetch_all:
                data = cursor.execute(query).fetchone()
            else:
                data = cursor.execute(query).fetchall()
        except sqlite3.OperationalError as e:
            if str(e).startswith('no such table'):
                raise SystemExit("Cache is empty")
            else:
                raise SystemExit('ERROR: {}. Query: {}'.format(e, query))
        conn.commit()
        conn.close()
        return data
    else:
        raise SystemExit('ERROR: Unacceptable SQL query: "{query}"'.format(query=query))


def get_skey(storage, hashed_login, use_cache=True):
    """
    Get's session key from HP MSA API.
    :param storage:
    String with storage IP address.
    :param hashed_login:
    str() Hashed with md5 login data.
    :param use_cache:
    The function will try to save session key to disk.
    :return:
    Session key as <str> or error code as <str>
    """

    # Global variable points to use HTTPS or not
    msa_ip = gethostbyname(storage)

    # Trying to use cached session key
    if use_cache:
        cur_timestamp = datetime.timestamp(datetime.utcnow())
        if not use_https:  # use http
            cache_data = sql_op('SELECT expired,skey FROM skey_cache WHERE ip="{}" AND proto="http"'.format(storage))
        else:  # use https
            cache_data = sql_op('SELECT expired,skey '
                                'FROM skey_cache '
                                'WHERE dns_name="{}" AND IP ="{}" AND proto="https"'.format(storage, msa_ip))
        if cache_data is not None:
            cache_expired, cached_skey = cache_data
            if cur_timestamp < float(cache_expired):
                return cached_skey
            else:
                return get_skey(storage, hashed_login, use_cache=False)
        else:
            return get_skey(storage, hashed_login, use_cache=False)
    else:
        # Forming URL and trying to make GET query
        login_url = '{strg}/api/login/{hash}'.format(strg=storage, hash=hashed_login)

        # Unpacking data from API
        return_code, sessionkey, xml_data = query_xmlapi(url=login_url, sessionkey=None)

        # 1 - success, write cache in file and return session key
        if return_code == '1':
            expired_time = datetime.timestamp(datetime.utcnow() + timedelta(minutes=15))
            if not use_https:  # http
                if sql_op('SELECT ip FROM skey_cache WHERE ip = "{ip}" AND proto="http"'.format(ip=storage)) is None:
                    # Inserting new cache
                    sql_op('INSERT INTO skey_cache (dns_name, ip, proto, expired, skey)'
                           'VALUES("{dns}", "{ip}", "http", "{time}", "{skey}")'.format(dns=storage,
                                                                                        ip=storage,
                                                                                        time=expired_time,
                                                                                        skey=sessionkey))
                else:
                    # Updating existing cache
                    sql_op('UPDATE skey_cache SET skey="{skey}", expired="{expired}" '
                           'WHERE ip="{ip}" AND proto="http"'.format(skey=sessionkey, expired=expired_time, ip=storage))
            else:  # https
                if sql_op('SELECT dns_name, ip FROM skey_cache '
                          'WHERE dns_name="{dns_name}" AND ip="{ip}" AND proto="https"'.format(dns_name=storage,
                                                                                               ip=msa_ip)) is None:
                    sql_op('INSERT INTO skey_cache (dns_name, ip, proto, expired, skey) '
                           'VALUES ("{name}", "{ip}", "https", "{expired}", "{skey}")'.format(name=storage,
                                                                                              ip=msa_ip,
                                                                                              expired=expired_time,
                                                                                              skey=sessionkey))
                else:
                    sql_op('UPDATE skey_cache SET skey = "{skey}", expired = "{expired}" '
                           'WHERE dns_name="{name}" AND ip="{ip}" AND proto="https"'.format(skey=sessionkey,
                                                                                            expired=expired_time,
                                                                                            name=storage, ip=msa_ip))
            return sessionkey
        # 2 - Authentication Unsuccessful, return "2"
        elif return_code == '2':
            return return_code


def query_xmlapi(url, sessionkey):
    """
    Making HTTP request to HP MSA XML API and returns it's response as 3-element tuple.
    :param url:
    URL to make GET request in <str>.
    :param sessionkey:
    Session key to authorize in <str>.
    :return:
    Tuple with return code <str>, return description <str> and etree object <xml.etree.ElementTree.Element>.
    """

    # Helps with debug info
    cur_fname = query_xmlapi.__name__

    # Set file where we can find root CA for our storages
    ca_file = '/etc/ssl/certs/ca-bundle.crt'

    # Makes GET request to URL
    try:
        if not use_https:  # http
            url = 'http://' + url
            response = requests.get(url, headers={'sessionKey': sessionkey})
        else:  # https
            url = 'https://' + url
            if verify_ssl:
                response = requests.get(url, headers={'sessionKey': sessionkey}, verify=ca_file)
            else:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = requests.get(url, headers={'sessionKey': sessionkey}, verify=False)
    except requests.exceptions.SSLError:
        raise SystemExit('ERROR: Cannot verify storage SSL Certificate.')
    except requests.exceptions.ConnectionError:
        raise SystemExit("ERROR: Cannot connect to storage.")

    # Reading data from server XML response
    try:
        response_xml = eTree.fromstring(response.content)

        # Parse result XML to get return code and description
        return_code = response_xml.find("./OBJECT[@name='status']/PROPERTY[@name='return-code']").text
        return_response = response_xml.find("./OBJECT[@name='status']/PROPERTY[@name='response']").text

        # Placing all data to the tuple which will be returned
        return return_code, return_response, response_xml
    except (ValueError, AttributeError) as e:
        raise SystemExit("ERROR: {f} : Cannot parse XML. {exc}".format(f=cur_fname, exc=e))


def get_health(storage, component, item, sessionkey):
    """
    The function gets single item of MSA component. E.g. - status of one disk. It may be useful for Zabbix < 3.4.
    :param storage:
    String with storage name in DNS or it's IP address.
    :param sessionkey:
    String with session key, which must be attach to the request header.
    :param component:
    Name of storage component, what we want to get - vdisks, disks, etc.
    :param item:
    ID number of getting component - number of disk, name of vdisk, etc.
    :return:
    HTTP response text in XML format.
    """

    # Forming URL
    if component in ('vdisks', 'disks'):
        get_url = '{strg}/api/show/{comp}/{item}'.format(strg=storage, comp=component, item=item)
    elif component in ('controllers', 'enclosures'):
        get_url = '{strg}/api/show/{comp}'.format(strg=storage, comp=component)
    else:
        raise SystemExit('ERROR: Wrong component "{comp}"'.format(comp=component))

    # Making request to API
    resp_return_code, resp_description, resp_xml = query_xmlapi(get_url, sessionkey)
    if resp_return_code != '0':
        raise SystemExit('ERROR: {rc} : {rd}'.format(rc=resp_return_code, rd=resp_description))

    # Matching dict
    md = {'controllers': 'controller-id', 'enclosures': 'enclosure-id', 'vdisks': 'virtual-disk', 'disks': 'drive'}
    # Returns health statuses
    # disks and vdisks
    if component in ('vdisks', 'disks'):
        try:
            health = resp_xml.find("./OBJECT[@name='{nd}']/PROPERTY[@name='health']".format(nd=md[component])).text
        except AttributeError:
            raise SystemExit("ERROR: No such id: '{item}'".format(item=item))
    # controllers and enclosures
    elif component in ('controllers', 'enclosures'):
        # we'll make dict {ctrl_id: health} because of we cannot call API for exact controller status
        health_dict = {}
        for ctrl in resp_xml.findall("./OBJECT[@name='{comp}']".format(comp=component)):
            ctrl_id = ctrl.find("./PROPERTY[@name='{nd}']".format(nd=md[component])).text
            # Add 'health' to dict
            health_dict[ctrl_id] = ctrl.find("./PROPERTY[@name='health']").text
        # If given item presents in our dict - return status
        if item in health_dict:
            health = health_dict[item]
        else:
            raise SystemExit("ERROR: No such id: '{item}'.".format(item=item))
    else:
        raise SystemExit("ERROR: Wrong component '{comp}'".format(comp=component))
    return health


def make_discovery(storage, component, sessionkey):
    """
    :param storage:
    String with storage name in DNS or it's IP address.
    :param sessionkey:
    String with session key, which must be attach to the request header.
    :param component:
    Name of storage component, what we want to get - vdisks, disks, etc.
    :return:
    JSON with discovery data.
    """

    # Forming URL
    show_url = '{strg}/api/show/{comp}'.format(strg=storage, comp=component)

    # Making request to API
    resp_return_code, resp_description, xml = query_xmlapi(show_url, sessionkey)
    if resp_return_code != '0':
        raise SystemExit('ERROR: {rc} : {rd}'.format(rc=resp_return_code, rd=resp_description))

    # Eject XML from response
    if component is not None:
        all_components = []
        raw_json_part = ''
        if component.lower() == 'vdisks':
            for vdisk in xml.findall("./OBJECT[@name='virtual-disk']"):
                vdisk_name = vdisk.find("./PROPERTY[@name='name']").text
                vdisk_dict = {"{#VDISKNAME}": "{name}".format(name=vdisk_name)}
                all_components.append(vdisk_dict)
        elif component.lower() == 'disks':
            for disk in xml.findall("./OBJECT[@name='drive']"):
                disk_loc = disk.find("./PROPERTY[@name='location']").text
                disk_sn = disk.find("./PROPERTY[@name='serial-number']").text
                disk_dict = {"{#DISKLOCATION}": "{loc}".format(loc=disk_loc),
                             "{#DISKSN}": "{sn}".format(sn=disk_sn)}
                all_components.append(disk_dict)
        elif component.lower() == 'controllers':
            for ctrl in xml.findall("./OBJECT[@name='controllers']"):
                ctrl_id = ctrl.find("./PROPERTY[@name='controller-id']").text
                ctrl_sn = ctrl.find("./PROPERTY[@name='serial-number']").text
                ctrl_ip = ctrl.find("./PROPERTY[@name='ip-address']").text
                # Find all possible ports
                fc_ports = {}
                for port in ctrl.findall("./OBJECT[@name='ports']"):
                    port_name = port.find("./PROPERTY[@name='port']").text
                    sfp_present = port.find(".//PROPERTY[@name='sfp-present-numeric']").text
                    fc_ports[port_name] = sfp_present
                for port, status in fc_ports.items():
                    raw_json_part += '{{"{{#PORTNAME}}":"{}","{{#SFPPRESENT}}":"{}"}},'.format(port, status)
                # Forming final dict
                ctrl_dict = {"{#CTRLID}": "{id}".format(id=ctrl_id),
                             "{#CTRLSN}": "{sn}".format(sn=ctrl_sn),
                             "{#CTRLIP}": "{ip}".format(ip=ctrl_ip)}
                all_components.append(ctrl_dict)
        elif component.lower() == 'enclosures':
            for encl in xml.findall(".OBJECT[@name='enclosures']"):
                encl_id = encl.find("./PROPERTY[@name='enclosure-id']").text
                encl_sn = encl.find("./PROPERTY[@name='midplane-serial-number']").text
                all_ps = [PS.find("./PROPERTY[@name='durable-id']").text
                          for PS in encl.findall("./OBJECT[@name='power-supplies']")]
                for ps in all_ps:
                    raw_json_part += '{{"{{#POWERSUPPLY}}":"{}"}},'.format(ps)
                # Forming final dict
                encl_dict = {"{#ENCLOSUREID}": "{id}".format(id=encl_id),
                             "{#ENCLOSURESN}": "{sn}".format(sn=encl_sn)}
                all_components.append(encl_dict)

        # Dumps JSON and return it
        if not raw_json_part:
            return json.dumps({"data": all_components}, separators=(',', ':'))
        else:
            return json.dumps({"data": all_components}, separators=(',', ':'))[:-2] + ',' + raw_json_part[:-1] + ']}'
    else:
        raise SystemExit('ERROR: You must provide the storage component (vdisks, disks, controllers, enclosures)')


def get_all(storage, component, sessionkey):
    """
    :param storage:
    String with storage name in DNS or it's IP address.
    :param sessionkey:
    String with session key, which must be attach to the request header.
    :param component:
    Name of storage component, what we want to get - vdisks, disks, etc.
    :return:
    JSON with all found data. For example:
    Disks:
    {"1.1": { "health": "OK", "temperature": 25, "work_hours": 1234}, "1.2": { ... }}
    Vdisks:
    {"vdisk01": { "health": "OK" }, vdisk02: {"health": "OK"} }
    """

    get_url = '{strg}/api/show/{comp}/'.format(strg=storage, comp=component)

    # Making request to API
    resp_return_code, resp_description, xml = query_xmlapi(get_url, sessionkey)
    if resp_return_code != '0':
        raise SystemExit('ERROR: {rc} : {rd}'.format(rc=resp_return_code, rd=resp_description))

    # Processing XML if response code 0
    all_components = {}
    if component == 'disks':
        for PROP in xml.findall("./OBJECT[@name='drive']"):
            # Getting data from XML
            disk_location = PROP.find("./PROPERTY[@name='location']").text
            disk_health = PROP.find("./PROPERTY[@name='health']").text
            disk_temp = PROP.find("./PROPERTY[@name='temperature-numeric']").text
            disk_work_hours = PROP.find("./PROPERTY[@name='power-on-hours']").text
            # Making dict with one disk data
            disk_info = {
                    "health": disk_health,
                    "temperature": disk_temp,
                    "work_hours": disk_work_hours
            }
            # Adding one disk to common dict
            all_components[disk_location] = disk_info
    elif component == 'vdisks':
        for PROP in xml.findall("./OBJECT[@name='virtual-disk']"):
            # Getting data from XML
            vdisk_name = PROP.find("./PROPERTY[@name='name']").text
            vdisk_health = PROP.find("./PROPERTY[@name='health']").text

            # Making dict with one vdisk data
            vdisk_info = {
                    "health": vdisk_health
            }
            # Adding one vdisk to common dict
            all_components[vdisk_name] = vdisk_info
    elif component == 'controllers':
        for PROP in xml.findall("./OBJECT[@name='controllers']"):
            # Getting data from XML
            ctrl_id = PROP.find("./PROPERTY[@name='controller-id']").text
            ctrl_health = PROP.find("./PROPERTY[@name='health']").text
            cf_health = PROP.find("./OBJECT[@basetype='compact-flash']/PROPERTY[@name='health']").text
            # Getting info for all FC ports
            ports_info = {}
            for FC_PORT in PROP.findall("./OBJECT[@name='ports']"):
                port_name = FC_PORT.find("./PROPERTY[@name='port']").text
                port_health = FC_PORT.find("./PROPERTY[@name='health']").text
                port_status = FC_PORT.find("./PROPERTY[@name='status']").text
                sfp_status = FC_PORT.find("./OBJECT[@name='port-details']/PROPERTY[@name='sfp-status']").text
                # Puts all info into dict
                ports_info[port_name] = {
                    "health": port_health,
                    "status": port_status,
                    "sfp_status": sfp_status
                }
                # Making final dict with info of the one controller
                ctrl_info = {
                    "health": ctrl_health,
                    "cf_health": cf_health,
                    "ports": ports_info
                }
                all_components[ctrl_id] = ctrl_info
    elif component == 'enclosures':
        for PROP in xml.findall("./OBJECT[@name='enclosures']"):
            encl_id = PROP.find("./PROPERTY[@name='enclosure-id']").text
            encl_health = PROP.find("./PROPERTY[@name='health']").text
            encl_status = PROP.find("./PROPERTY[@name='status']").text
            # Power supply info
            ps_info = {}
            for PS in PROP.findall("./OBJECT[@name='power-supplies']"):
                ps_id = PS.find("./PROPERTY[@name='durable-id']").text
                ps_name = PS.find("./PROPERTY[@name='name']").text
                ps_health = PS.find("./PROPERTY[@name='health']").text
                ps_status = PS.find("./PROPERTY[@name='status']").text
                ps_temp = PS.find("./PROPERTY[@name='dctemp']").text
                # Puts all info into dict
                ps_info[ps_id] = {
                    "name": ps_name,
                    "health": ps_health,
                    "status": ps_status,
                    "temperature": ps_temp
                }
                # Making final dict with info of the one controller
                encl_info = {
                    "health": encl_health,
                    "status": encl_status,
                    "power_supplies": ps_info
                }
                all_components[encl_id] = encl_info
    else:
        raise SystemExit('ERROR: You should provide the storage component (vdisks, disks, controllers)')
    # Making JSON with dumps() and return it (separators needs to make JSON compact)
    return json.dumps(all_components, separators=(',', ':'))


if __name__ == '__main__':
    # Current program version
    VERSION = '0.4'

    # Parse all given arguments
    parser = ArgumentParser(description='Zabbix module for HP MSA XML API.', add_help=True)
    parser.add_argument('-d', '--discovery', action='store_true', help='Making discovery')
    parser.add_argument('-g', '--get', '--health', type=str, help='ID of MSA part which status we want to get',
                        metavar='[DISKID|VDISKNAME|CONTROLLERID|ENCLOSUREID|all]')
    parser.add_argument('-u', '--user', default='monitor', type=str, help='User name to login in MSA')
    parser.add_argument('-p', '--password', default='!monitor', type=str, help='Password for your user')
    parser.add_argument('-f', '--loginfile', type=str, help='Path to file contains login and password')
    parser.add_argument('-m', '--msa', type=str, help='DNS name or IP address of your MSA controller',
                        metavar='[IP|DNSNAME]')
    parser.add_argument('-c', '--component', type=str, choices=['disks', 'vdisks', 'controllers', 'enclosures'],
                        help='MSA component for monitor or discover',
                        metavar='[disks|vdisks|controllers|enclosures]')
    parser.add_argument('--https', type=str, choices=['direct', 'verify'], help='Use https instead http',
                        metavar='[direct|verify]')
    parser.add_argument('-v', '--version', action='version', version=VERSION, help='Print the script version and exit')
    parser.add_argument('--showcache', action='store_true', help='Display cache data')
    args = parser.parse_args()

    # Create cache table if it not exists
    sql_op('CREATE TABLE IF NOT EXISTS skey_cache ('
           'dns_name TEXT NOT NULL, '
           'ip TEXT NOT NULL, '
           'proto TEXT NOT NULL, '
           'expired TEXT NOT NULL, '
           'skey TEXT NOT NULL DEFAULT 0, '
           'PRIMARY KEY (dns_name, ip, proto))')

    # Display cache data and exit
    if args.showcache:
        print("{:^30} {:^15} {:^7} {:^19} {:^32}".format('hostname', 'ip', 'proto', 'expired', 'sessionkey'))
        print("{:-^30} {:-^15} {:-^7} {:-^19} {:-^32}".format('-', '-', '-', '-', '-'))
        for cache in sql_op('SELECT * FROM skey_cache', fetch_all=True):
            name, ip, proto, expired, skey = cache
            print("{:30} {:15} {:^7} {:19} {:32}".format(
                name, ip, proto, datetime.fromtimestamp(float(expired)).strftime("%H:%M:%S %d.%m.%Y"), skey))
        exit(0)

    # Make no possible to use '--discovery' and '--get' options together
    if args.discovery and args.get:
        raise SystemExit("Syntax error: Cannot use '-d|--discovery' and '-g|--get' options together.")

    # Set msa_connect - IP or DNS name and determine to use https or not
    use_https = args.https in ['direct', 'verify']
    verify_ssl = args.https == 'verify'
    msa_connect = args.msa if verify_ssl else gethostbyname(args.msa)

    # Make no possible to use '--discovery' and '--get' options together
    if args.discovery and args.get:
        raise SystemExit("Syntax error: Cannot use '-d|--discovery' and '-g|--get' options together.")

    # Make login hash string
    if args.loginfile:
        cred_hash = hash_pwd(args.loginfile, isfile=True)
    else:
        login_string = '_'.join([args.user, args.password])
        cred_hash = hash_pwd(login_string)

    # Getting sessionkey
    skey = get_skey(msa_connect, cred_hash)

    # If gets '--discovery' argument, make discovery
    if args.discovery:
        print(make_discovery(msa_connect, args.component, skey))
    # If gets '--get' argument, getting component's health
    elif args.get and args.get != 'all':
        print(get_health(msa_connect, args.component, args.get, skey))
    # Making bulk request for all possible component statuses
    elif args.get == 'all':
        print(get_all(msa_connect, args.component, skey))
    else:
        raise SystemExit("Syntax error: You must use '--discovery' or '--get' option anyway.")
