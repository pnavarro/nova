# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2012 NTT DOCOMO, INC.
# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Defines interface for DB access.

The underlying driver is loaded as a :class:`LazyPluggable`.

Functions in this module are imported into the nova.virt.baremetal.db
namespace. Call these functions from nova.virt.baremetal.db namespace, not
the nova.virt.baremetal.db.api namespace.

All functions in this module return objects that implement a dictionary-like
interface. Currently, many of these objects are sqlalchemy objects that
implement a dictionary interface. However, a future goal is to have all of
these objects be simple dictionaries.


**Related Flags**

:baremetal_db_backend:  string to lookup in the list of LazyPluggable backends.
                        `sqlalchemy` is the only supported backend right now.

:baremetal_sql_connection:  string specifying the sqlalchemy connection to use,
                            like: `sqlite:///var/lib/nova/nova.sqlite`.

"""

from nova import config
from nova.openstack.common import cfg
from nova import utils


db_opts = [
    cfg.StrOpt('baremetal_db_backend',
               default='sqlalchemy',
               help='The backend to use for db'),
    ]

CONF = config.CONF
CONF.register_opts(db_opts)

IMPL = utils.LazyPluggable(
        'baremetal_db_backend',
        sqlalchemy='nova.virt.baremetal.db.sqlalchemy.api')


def bm_node_get_all(context, service_host=None):
    return IMPL.bm_node_get_all(context,
                                service_host=service_host)


def bm_node_find_free(context, service_host=None,
                      memory_mb=None, cpus=None, local_gb=None):
    return IMPL.bm_node_find_free(context,
                                  service_host=service_host,
                                  memory_mb=memory_mb,
                                  cpus=cpus,
                                  local_gb=local_gb)


def bm_node_get(context, bm_node_id):
    return IMPL.bm_node_get(context, bm_node_id)


def bm_node_get_by_instance_uuid(context, instance_uuid):
    return IMPL.bm_node_get_by_instance_uuid(context,
                                             instance_uuid)


def bm_node_create(context, values):
    return IMPL.bm_node_create(context, values)


def bm_node_destroy(context, bm_node_id):
    return IMPL.bm_node_destroy(context, bm_node_id)


def bm_node_update(context, bm_node_id, values):
    return IMPL.bm_node_update(context, bm_node_id, values)


def bm_pxe_ip_create(context, address, server_address):
    return IMPL.bm_pxe_ip_create(context, address, server_address)


def bm_pxe_ip_create_direct(context, bm_pxe_ip):
    return IMPL.bm_pxe_ip_create_direct(context, bm_pxe_ip)


def bm_pxe_ip_destroy(context, ip_id):
    return IMPL.bm_pxe_ip_destroy(context, ip_id)


def bm_pxe_ip_destroy_by_address(context, address):
    return IMPL.bm_pxe_ip_destroy_by_address(context, address)


def bm_pxe_ip_get_all(context):
    return IMPL.bm_pxe_ip_get_all(context)


def bm_pxe_ip_get(context, ip_id):
    return IMPL.bm_pxe_ip_get(context, ip_id)


def bm_pxe_ip_get_by_bm_node_id(context, bm_node_id):
    return IMPL.bm_pxe_ip_get_by_bm_node_id(context, bm_node_id)


def bm_pxe_ip_associate(context, bm_node_id):
    return IMPL.bm_pxe_ip_associate(context, bm_node_id)


def bm_pxe_ip_disassociate(context, bm_node_id):
    return IMPL.bm_pxe_ip_disassociate(context, bm_node_id)


def bm_interface_get(context, if_id):
    return IMPL.bm_interface_get(context, if_id)


def bm_interface_get_all(context):
    return IMPL.bm_interface_get_all(context)


def bm_interface_destroy(context, if_id):
    return IMPL.bm_interface_destroy(context, if_id)


def bm_interface_create(context, bm_node_id, address, datapath_id, port_no):
    return IMPL.bm_interface_create(context, bm_node_id, address,
                                    datapath_id, port_no)


def bm_interface_set_vif_uuid(context, if_id, vif_uuid):
    return IMPL.bm_interface_set_vif_uuid(context, if_id, vif_uuid)


def bm_interface_get_by_vif_uuid(context, vif_uuid):
    return IMPL.bm_interface_get_by_vif_uuid(context, vif_uuid)


def bm_interface_get_all_by_bm_node_id(context, bm_node_id):
    return IMPL.bm_interface_get_all_by_bm_node_id(context, bm_node_id)


def bm_deployment_create(context, key, image_path, pxe_config_path, root_mb,
                         swap_mb):
    return IMPL.bm_deployment_create(context, key, image_path,
                                     pxe_config_path, root_mb, swap_mb)


def bm_deployment_get(context, dep_id):
    return IMPL.bm_deployment_get(context, dep_id)


def bm_deployment_destroy(context, dep_id):
    return IMPL.bm_deployment_destroy(context, dep_id)
