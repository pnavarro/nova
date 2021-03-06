# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Michael Still and Canonical Inc
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

"""Image cache manager.

The cache manager implements the specification at
http://wiki.openstack.org/nova-image-cache-management.

"""

import hashlib
import os
import re
import time

from nova.compute import task_states
from nova.compute import vm_states
from nova.openstack.common import cfg
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova import utils
from nova.virt.libvirt import utils as virtutils


LOG = logging.getLogger(__name__)

imagecache_opts = [
    cfg.BoolOpt('remove_unused_base_images',
                default=True,
                help='Should unused base images be removed?'),
    cfg.IntOpt('remove_unused_resized_minimum_age_seconds',
               default=3600,
               help='Unused resized base images younger than this will not be '
                    'removed'),
    cfg.IntOpt('remove_unused_original_minimum_age_seconds',
               default=(24 * 3600),
               help='Unused unresized base images younger than this will not '
                    'be removed'),
    cfg.BoolOpt('checksum_base_images',
                default=False,
                help='Write a checksum for files in _base to disk'),
    cfg.IntOpt('checksum_interval_seconds',
               default=3600,
               help='How frequently to checksum base images'),
    ]

CONF = cfg.CONF
CONF.register_opts(imagecache_opts)
CONF.import_opt('host', 'nova.config')
CONF.import_opt('instances_path', 'nova.compute.manager')
CONF.import_opt('base_dir_name', 'nova.compute.manager')


def read_stored_checksum(target, timestamped=True):
    """Read the checksum.

    Returns the checksum (as hex) or None.
    """
    return virtutils.read_stored_info(target, field='sha1',
                                      timestamped=timestamped)


def write_stored_checksum(target):
    """Write a checksum to disk for a file in _base."""

    with open(target, 'r') as img_file:
        checksum = utils.hash_file(img_file)
    virtutils.write_stored_info(target, field='sha1', value=checksum)


class ImageCacheManager(object):
    def __init__(self):
        self.lock_path = os.path.join(CONF.instances_path, 'locks')
        self._reset_state()

    def _reset_state(self):
        """Reset state variables used for each pass."""

        self.used_images = {}
        self.image_popularity = {}
        self.instance_names = {}

        self.active_base_files = []
        self.corrupt_base_files = []
        self.originals = []
        self.removable_base_files = []
        self.unexplained_images = []

    def _store_image(self, base_dir, ent, original=False):
        """Store a base image for later examination."""
        entpath = os.path.join(base_dir, ent)
        if os.path.isfile(entpath):
            self.unexplained_images.append(entpath)
            if original:
                self.originals.append(entpath)

    def _list_base_images(self, base_dir):
        """Return a list of the images present in _base.

        Determine what images we have on disk. There will be other files in
        this directory (for example kernels) so we only grab the ones which
        are the right length to be disk images.

        Note that this does not return a value. It instead populates a class
        variable with a list of images that we need to try and explain.
        """
        digest_size = hashlib.sha1().digestsize * 2
        for ent in os.listdir(base_dir):
            if len(ent) == digest_size:
                self._store_image(base_dir, ent, original=True)

            elif (len(ent) > digest_size + 2 and
                  ent[digest_size] == '_' and
                  not virtutils.is_valid_info_file(os.path.join(base_dir,
                                                                ent))):
                self._store_image(base_dir, ent, original=False)

    def _list_running_instances(self, context, all_instances):
        """List running instances (on all compute nodes)."""
        self.used_images = {}
        self.image_popularity = {}
        self.instance_names = set()

        for instance in all_instances:
            self.instance_names.add(instance['name'])

            resize_states = [task_states.RESIZE_PREP,
                             task_states.RESIZE_MIGRATING,
                             task_states.RESIZE_MIGRATED,
                             task_states.RESIZE_FINISH]
            if instance['task_state'] in resize_states or \
                instance['vm_state'] == vm_states.RESIZED:
                self.instance_names.add(instance['name'] + '_resize')

            image_ref_str = str(instance['image_ref'])
            local, remote, insts = self.used_images.get(image_ref_str,
                                                        (0, 0, []))
            if instance['host'] == CONF.host:
                local += 1
            else:
                remote += 1
            insts.append(instance['name'])
            self.used_images[image_ref_str] = (local, remote, insts)

            self.image_popularity.setdefault(image_ref_str, 0)
            self.image_popularity[image_ref_str] += 1

    def _list_backing_images(self):
        """List the backing images currently in use."""
        inuse_images = []
        for ent in os.listdir(CONF.instances_path):
            if ent in self.instance_names:
                LOG.debug(_('%s is a valid instance name'), ent)
                disk_path = os.path.join(CONF.instances_path, ent, 'disk')
                if os.path.exists(disk_path):
                    LOG.debug(_('%s has a disk file'), ent)
                    backing_file = virtutils.get_disk_backing_file(disk_path)
                    LOG.debug(_('Instance %(instance)s is backed by '
                                '%(backing)s'),
                              {'instance': ent,
                               'backing': backing_file})

                    if backing_file:
                        backing_path = os.path.join(CONF.instances_path,
                                                    CONF.base_dir_name,
                                                    backing_file)
                        if not backing_path in inuse_images:
                            inuse_images.append(backing_path)

                        if backing_path in self.unexplained_images:
                            LOG.warning(_('Instance %(instance)s is using a '
                                          'backing file %(backing)s which '
                                          'does not appear in the image '
                                          'service'),
                                        {'instance': ent,
                                         'backing': backing_file})
                            self.unexplained_images.remove(backing_path)

        return inuse_images

    def _find_base_file(self, base_dir, fingerprint):
        """Find the base file matching this fingerprint.

        Yields the name of the base file, a boolean which is True if the image
        is "small", and a boolean which indicates if this is a resized image.
        Note that is is possible for more than one yield to result from this
        check.

        If no base file is found, then nothing is yielded.
        """
        # The original file from glance
        base_file = os.path.join(base_dir, fingerprint)
        if os.path.exists(base_file):
            yield base_file, False, False

        # An older naming style which can be removed sometime after Folsom
        base_file = os.path.join(base_dir, fingerprint + '_sm')
        if os.path.exists(base_file):
            yield base_file, True, False

        # Resized images
        resize_re = re.compile('.*/%s_[0-9]+$' % fingerprint)
        for img in self.unexplained_images:
            m = resize_re.match(img)
            if m:
                yield img, False, True

    def _verify_checksum(self, img_id, base_file, create_if_missing=True):
        """Compare the checksum stored on disk with the current file.

        Note that if the checksum fails to verify this is logged, but no actual
        action occurs. This is something sysadmins should monitor for and
        handle manually when it occurs.
        """

        if not CONF.checksum_base_images:
            return None

        lock_name = 'hash-%s' % os.path.split(base_file)[-1]

        # Protect against other nova-computes performing checksums at the same
        # time if we are using shared storage
        @lockutils.synchronized(lock_name, 'nova-', external=True,
                                lock_path=self.lock_path)
        def inner_verify_checksum():
            (stored_checksum, stored_timestamp) = read_stored_checksum(
                base_file, timestamped=True)
            if stored_checksum:
                # NOTE(mikal): Checksums are timestamped. If we have recently
                # checksummed (possibly on another compute node if we are using
                # shared storage), then we don't need to checksum again.
                if (stored_timestamp and
                    time.time() - stored_timestamp <
                    CONF.checksum_interval_seconds):
                    return True

                # NOTE(mikal): If there is no timestamp, then the checksum was
                # performed by a previous version of the code.
                if not stored_timestamp:
                    virtutils.write_stored_info(base_file, field='sha1',
                                                value=stored_checksum)

                with open(base_file, 'r') as f:
                    current_checksum = utils.hash_file(f)

                if current_checksum != stored_checksum:
                    LOG.error(_('image %(id)s at (%(base_file)s): image '
                                'verification failed'),
                              {'id': img_id,
                               'base_file': base_file})
                    return False

                else:
                    return True

            else:
                LOG.info(_('image %(id)s at (%(base_file)s): image '
                           'verification skipped, no hash stored'),
                         {'id': img_id,
                          'base_file': base_file})

                # NOTE(mikal): If the checksum file is missing, then we should
                # create one. We don't create checksums when we download images
                # from glance because that would delay VM startup.
                if CONF.checksum_base_images and create_if_missing:
                    LOG.info(_('%(id)s (%(base_file)s): generating checksum'),
                             {'id': img_id,
                              'base_file': base_file})
                    write_stored_checksum(base_file)

                return None

        return inner_verify_checksum()

    def _remove_base_file(self, base_file):
        """Remove a single base file if it is old enough.

        Returns nothing.
        """
        if not os.path.exists(base_file):
            LOG.debug(_('Cannot remove %(base_file)s, it does not exist'),
                      base_file)
            return

        mtime = os.path.getmtime(base_file)
        age = time.time() - mtime

        maxage = CONF.remove_unused_resized_minimum_age_seconds
        if base_file in self.originals:
            maxage = CONF.remove_unused_original_minimum_age_seconds

        if age < maxage:
            LOG.info(_('Base file too young to remove: %s'),
                     base_file)
        else:
            LOG.info(_('Removing base file: %s'), base_file)
            try:
                os.remove(base_file)
                signature = virtutils.get_info_filename(base_file)
                if os.path.exists(signature):
                    os.remove(signature)
            except OSError, e:
                LOG.error(_('Failed to remove %(base_file)s, '
                            'error was %(error)s'),
                          {'base_file': base_file,
                           'error': e})

    def _handle_base_image(self, img_id, base_file):
        """Handle the checks for a single base image."""

        image_bad = False
        image_in_use = False

        LOG.info(_('image %(id)s at (%(base_file)s): checking'),
                 {'id': img_id,
                  'base_file': base_file})

        if base_file in self.unexplained_images:
            self.unexplained_images.remove(base_file)

        if (base_file and os.path.exists(base_file)
            and os.path.isfile(base_file)):
            # _verify_checksum returns True if the checksum is ok, and None if
            # there is no checksum file
            checksum_result = self._verify_checksum(img_id, base_file)
            if not checksum_result is None:
                image_bad = not checksum_result

            # Give other threads a chance to run
            time.sleep(0)

        instances = []
        if img_id in self.used_images:
            local, remote, instances = self.used_images[img_id]

            if local > 0 or remote > 0:
                image_in_use = True
                LOG.info(_('image %(id)s at (%(base_file)s): '
                           'in use: on this node %(local)d local, '
                           '%(remote)d on other nodes sharing this instance '
                           'storage'),
                         {'id': img_id,
                          'base_file': base_file,
                          'local': local,
                          'remote': remote})

                self.active_base_files.append(base_file)

                if not base_file:
                    LOG.warning(_('image %(id)s at (%(base_file)s): warning '
                                  '-- an absent base file is in use! '
                                  'instances: %(instance_list)s'),
                                {'id': img_id,
                                 'base_file': base_file,
                                 'instance_list': ' '.join(instances)})

        if image_bad:
            self.corrupt_base_files.append(base_file)

        if base_file:
            if not image_in_use:
                LOG.debug(_('image %(id)s at (%(base_file)s): image is not in '
                            'use'),
                          {'id': img_id,
                           'base_file': base_file})
                self.removable_base_files.append(base_file)

            else:
                LOG.debug(_('image %(id)s at (%(base_file)s): image is in '
                            'use'),
                          {'id': img_id,
                           'base_file': base_file})
                if os.path.exists(base_file):
                    virtutils.chown(base_file, os.getuid())
                    os.utime(base_file, None)

    def verify_base_images(self, context, all_instances):
        """Verify that base images are in a reasonable state."""

        # NOTE(mikal): The new scheme for base images is as follows -- an
        # image is streamed from the image service to _base (filename is the
        # sha1 hash of the image id). If CoW is enabled, that file is then
        # resized to be the correct size for the instance (filename is the
        # same as the original, but with an underscore and the resized size
        # in bytes). This second file is then CoW'd to the instance disk. If
        # CoW is disabled, the resize occurs as part of the copy from the
        # cache to the instance directory. Files ending in _sm are no longer
        # created, but may remain from previous versions.
        self._reset_state()

        base_dir = os.path.join(CONF.instances_path, CONF.base_dir_name)
        if not os.path.exists(base_dir):
            LOG.debug(_('Skipping verification, no base directory at %s'),
                      base_dir)
            return

        LOG.debug(_('Verify base images'))
        self._list_base_images(base_dir)
        self._list_running_instances(context, all_instances)

        # Determine what images are on disk because they're in use
        for img in self.used_images:
            fingerprint = hashlib.sha1(img).hexdigest()
            LOG.debug(_('Image id %(id)s yields fingerprint %(fingerprint)s'),
                      {'id': img,
                       'fingerprint': fingerprint})
            for result in self._find_base_file(base_dir, fingerprint):
                base_file, image_small, image_resized = result
                self._handle_base_image(img, base_file)

                if not image_small and not image_resized:
                    self.originals.append(base_file)

        # Elements remaining in unexplained_images might be in use
        inuse_backing_images = self._list_backing_images()
        for backing_path in inuse_backing_images:
            if not backing_path in self.active_base_files:
                self.active_base_files.append(backing_path)

        # Anything left is an unknown base image
        for img in self.unexplained_images:
            LOG.warning(_('Unknown base file: %s'), img)
            self.removable_base_files.append(img)

        # Dump these lists
        if self.active_base_files:
            LOG.info(_('Active base files: %s'),
                     ' '.join(self.active_base_files))
        if self.corrupt_base_files:
            LOG.info(_('Corrupt base files: %s'),
                     ' '.join(self.corrupt_base_files))

        if self.removable_base_files:
            LOG.info(_('Removable base files: %s'),
                     ' '.join(self.removable_base_files))

            if CONF.remove_unused_base_images:
                for base_file in self.removable_base_files:
                    self._remove_base_file(base_file)

        # That's it
        LOG.debug(_('Verification complete'))
