# encoding = utf-8
"""
//
// AzureMonitorAddonForSplunk
//
// Copyright (c) Microsoft Corporation
//
// All rights reserved.
//
// MIT License
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the ""Software""), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is furnished
// to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.

// THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
// FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
// COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
// IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
// WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
//
"""
import sys
from timewindow import put_time_window, put_time_checkpoint
from concurrent import futures
from subs import get_subscription_segment, get_resources, get_azure_environment, \
    get_access_token, get_metrics_for_resources, get_secret_from_keyvault

MASK = '********'

def create_or_update_storage_password(self, props, logger):
    '''
        unencrypted password in inputs.conf, encrypt it and store as storagePassword
    '''
    try:

        locale = 'reference'
        storage_passwords = self.service.storage_passwords
        if props['username'] in storage_passwords:
            locale = 'delete'
            storage_passwords.delete(props['username'])

    except Exception as e:
        logger('ERROR', 'Error at locale {1} in create_or_update_storage_password: {0}'\
            .format(e, locale))

    try:
        locale = 'create'
        self.service.storage_passwords.create(props['password'], props['username'])
    except Exception as e:
        logger('ERROR', 'Error at locale {1} in create_or_update_storage_password: {0}'\
            .format(e, locale))


def mask_id_and_key(self, name, logger):
    '''
        masks the app_id and app_key in inputs.conf
    '''
    kind, input_name = name.split('://')
    item = self.service.inputs.__getitem__((input_name, kind))

    try:

        new_input = {
            'vaultName': item.content.vaultName,
            'SPNTenantID': item.content.SPNTenantID,
            'SPNApplicationId': MASK,
            'SPNApplicationKey': MASK,
            'SubscriptionId': item.content.SubscriptionId,
            'secretName': item.content.secretName,
            'secretVersion': item.content.secretVersion,
            'index': item.content.index,
            'interval': item.content.interval,
            'sourcetype': item.content.sourcetype
        }

        item.update(**new_input).refresh()

    except Exception as e:
        logger('ERROR', 'Error caught in mask_id_and_key: {0}'.format(e))

def get_or_store_secrets(self, inputs, logger):
    '''
        Either read existing encyrpted password or encrypt clear text password and store it
        Either way, return a set of clear text credentials
    '''
    input_items = inputs.inputs.itervalues().next()
    input_name = inputs.inputs.iterkeys().next()

    props_app_id = {}
    props_app_id['username'] = 'AzureMonitorMetricsAppID'
    props_app_id['password'] = input_items["SPNApplicationId"]

    props_app_key = {}
    props_app_key['username'] = 'AzureMonitorMetricsAppKey'
    props_app_key['password'] = input_items["SPNApplicationKey"]

    app_id = input_items["SPNApplicationId"]
    app_key = input_items["SPNApplicationKey"]

    try:
        if props_app_id['password'] == MASK:
            app_id, app_key = get_app_id_and_key(self, props_app_id, props_app_key, logger)
        else:
            create_or_update_storage_password(self, props_app_id, logger)
            create_or_update_storage_password(self, props_app_key, logger)
            mask_id_and_key(self, input_name, logger)
    except Exception as e:
        logger('ERROR', 'Error caught in get_or_store_secrets: {0}'.format(e))

    return app_id, app_key


def get_app_id_and_key(self, props_app_id, props_app_key, logger):
    '''
        get the encrypted app_id and app_key from storage_passwords
    '''
    storage_passwords = self.service.storage_passwords
    if props_app_id['username'] not in storage_passwords:
        raise KeyError('Did not find app_id {} in storage_passwords.'\
            .format(props_app_id['username']))

    if props_app_key['username'] not in storage_passwords:
        raise KeyError('Did not find app_id {} in storage_passwords.'\
            .format(props_app_key['username']))

    app_id = ''
    app_key = ''
    try:
        app_id = storage_passwords[props_app_id['username']].clear_password
        app_key = storage_passwords[props_app_key['username']].clear_password
    except Exception as e:
        logger('ERROR', 'Error caught in get_app_id_and_key: {0}'.format(e))

    return app_id, app_key


def get_resources_for_rgs(ew, bearer_token, sub_url, resource_groups, input_sourcetype, checkpoint_dict):
    """
        map the resource groups to a function that gets resources
    """
    resource_group_names = []
    for resource_group in resource_groups:
        resource_group_names.append(resource_group['name'])

    with futures.ThreadPoolExecutor(max_workers=5) as executor:
        rg_future = dict((executor.submit(get_resources, ew, bearer_token, sub_url, rg), rg)
                         for rg in resource_group_names)

        for future in futures.as_completed(rg_future, None):
            resource_group = rg_future[future]
            if future.exception() is not None:
                ew.log('ERROR', 'Resource group {0} generated an exception: {1}'
                       .format(resource_group, future.exception()))
            else:
                get_metrics_for_resources(ew, bearer_token, \
                    sub_url, resource_group, future.result(), input_sourcetype, checkpoint_dict)


def get_metrics_for_subscription(inputs, app_id, app_key, ew):
    """
        top level function
        given subscription id and credentials, get metrics for all resources with the right tags
        splunk sends an array of inputs, but only one element, hence the [0]
    """

    metadata = inputs.metadata
    input_name, input_item = inputs.inputs.popitem()
    stanza = input_name.split('://')
    instance_name = stanza[1]

    try:

        locale = "checkpoint file data"
        checkpoint_dir = metadata['checkpoint_dir']
        checkpoint_dict = {"checkpoint_dir":checkpoint_dir, "instance_name": instance_name}

        locale = "put_time_window"
        # update the time window for this iteration
        put_time_window(ew, checkpoint_dict)

        locale = "put_time_checkpoint"
        # and update the checkpoint for next time
        put_time_checkpoint(ew, checkpoint_dict)

        tenant_id = input_item["SPNTenantID"]
        spn_client_id = app_id
        spn_client_secret = app_key
        subscription_id = input_item["SubscriptionId"]
        key_vault_name = input_item["vaultName"]
        secret_name = input_item["secretName"]
        secret_version = input_item["secretVersion"]
        input_sourcetype = input_item["sourcetype"]

        locale = "get_access_token for key vault SPN"
        authentication_endpoint = "https://login.windows.net/"
        resource = 'https://vault.azure.net'
        kv_bearer_token = get_access_token(
            tenant_id,
            spn_client_id,
            spn_client_secret,
            authentication_endpoint,
            resource)

        locale = "get_secret_from_keyvault"
        arm_creds = get_secret_from_keyvault(ew, kv_bearer_token,
                                             key_vault_name, secret_name, secret_version)

        locale = "get_access_token"
        authentication_endpoint = get_azure_environment(
            'Azure')['activeDirectoryEndpointUrl']
        resource = get_azure_environment(
            'Azure')['activeDirectoryResourceId']
        bearer_token = get_access_token(
            tenant_id,
            arm_creds['spn_client_id'],
            arm_creds['spn_client_secret'],
            authentication_endpoint,
            resource)

        locale = "get_azure_environment"
        resource_mgr_endpoint_url = get_azure_environment(
            'Azure')['resourceManagerEndpointUrl']

        locale = "get_subscription_segment"
        sub_url = resource_mgr_endpoint_url + \
            get_subscription_segment(subscription_id)

        locale = "get_resources"
        resource_groups = get_resources(ew, bearer_token, sub_url)

        locale = "get_resources_for_rgs"
        get_resources_for_rgs(ew, bearer_token, sub_url, resource_groups, input_sourcetype, checkpoint_dict)

    except:
        ew.log('ERROR', 'Error caught in get_metrics_for_subscription, type: {0}, value: {1}, locale = {2}'
               .format(sys.exc_info()[0], sys.exc_info()[1], locale))
