#!/bin/python3

import xml.etree.ElementTree as eTree
from hashlib import md5
from urllib import request
from re import sub
from argparse import ArgumentParser


def get_skey(storage, login, password):
    """
    :param storage:
    String with storage name in DNS or it's IP address.
    :param login:
    String with MSA username.
    :param password:
    String with MSA password.
    :return:
    Session key as string.
    """

    # Combine login and password to 'login_password' format.
    login_data = '_'.join([login, password])
    login_hash = md5(login_data.encode()).hexdigest()
    login_url = 'http://{0}/api/login/{1}'.format(storage, login_hash)
    query_skey = request.urlopen(login_url).read()
    session_key = eTree.fromstring(query_skey.decode())[0][2].text
    return session_key


def get_value(storage, sessionkey, component, item):
    """
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

    url = 'http://{0}/api/show/{1}/{2}'.format(storage, component, item)
    req = request.Request(url)
    req.add_header('sessionKey', sessionkey)
    response = request.urlopen(req).read()
    response_xml = eTree.fromstring(response.decode())
    if component == 'vdisks' or component == 'disks':
        for obj in response_xml.findall('OBJECT'):
            if obj.attrib['name'] in ('virtual-disk', 'drive'):
                for prop in obj.iter('PROPERTY'):
                    if prop.get('display-name') == 'Health':
                        return prop.text
    # I know, we can't get something else because of choices in argparse, but why not?..
    else:
        return None


def make_discovery(storage, sessionkey, component):
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

    url = 'http://{0}/api/show/{1}'.format(storage, component)
    req = request.Request(url)
    req.add_header('sessionKey', sessionkey)
    response_xml = request.urlopen(req).read()
    discovery_xml = eTree.fromstring(response_xml.decode())
    if component is not None or len(component) != 0:
        json_body = ''
        if component == 'vdisks':
            for vdisk in discovery_xml.findall('OBJECT'):
                if vdisk.get('name') == 'virtual-disk':
                    for prop in vdisk.iter('PROPERTY'):
                        if prop.get('display-name') == 'Name':
                            vdisk_name = prop.text
                            json_body += '{{"{{#VDISKNAME}}":"{0}"}},'.format(vdisk_name)
            json_body = sub(r'},$', '', json_body) + '}'
            json_full = '{"data":[' + json_body + ']}'
            return json_full
        elif component == 'disks':
            for disk in discovery_xml.findall('OBJECT'):
                if disk.get('name') == 'drive':
                    for prop in disk.iter('PROPERTY'):
                        if prop.get('display-name') == 'Location':
                            disk_location = prop.text
                            json_body += '{{"{{#DISKLOCATION}}":"{0}",'.format(disk_location)
                        if prop.get('display-name') == 'Serial Number':
                            disk_sn = prop.text
                            json_body += '"{{#DISKSN}}":"{0}"}},'.format(disk_sn)
            json_body = sub(r',$', '', json_body)
            json_full = '{"data":[' + json_body + ']}'
            return json_full
    else:
        SystemExit('You should provide the storage component (vdisks, disks).')


if __name__ == '__main__':
    # Current program version
    VERSION = '0.2.3'
    # Parse all given arguments
    parser = ArgumentParser(description='Zabbix module for MSA XML API.', add_help=True)
    parser.add_argument('-d', '--discovery', action='store_true')
    parser.add_argument('-g', '--get', type=str, help='ID of part which status we want to get',
                        metavar='<DISKID> or <VDISKNAME>')
    parser.add_argument('-u', '--user', default='monitor', type=str, help='User name to login in MSA')
    parser.add_argument('-p', '--password', default='!monitor', type=str, help='Password for your user')
    parser.add_argument('-m', '--msa', type=str, help='DNS name or IP address of your MSA controller',
                        metavar='<IP> or <DNSNAME>')
    parser.add_argument('-c', '--component', type=str, choices=['disks', 'vdisks'], help='MSA component to monitor',
                        metavar='<disks> or <vdisks>')
    parser.add_argument('-v', '--version', action='version', version=VERSION, help='Just show program version')
    args = parser.parse_args()

    skey = get_skey(args.msa, args.user, args.password)
    if skey == 'Authentication Unsuccessful':
        raise SystemExit('ERROR: Login or password is incorrect.')
    # Parsing arguments
    # Make no possible to use '-d' and '-g' options together
    if args.discovery is True and args.get is not None:
        raise SystemExit("ERROR: Use cannot use both '--discovery' and '--get' options.")
    # If gets '--discovery' argument, make discovery
    elif args.discovery is True:
        print(make_discovery(args.msa, skey, args.component))
    # If gets '--get' argument, getting value of component
    elif args.get is not None and len(args.get) != 0:
        print(get_value(args.msa, skey, args.component, args.get))
    else:
        raise SystemExit("WARNING: You must use '--discovery' or '--get' option anyway.")
