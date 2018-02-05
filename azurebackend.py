# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
# This is modified version of Azure Backend from duplicity 0.7.16.
# Unlike the original one, it is trying to take advantage
# of tiering to get cheaper storage.
#
# https://bazaar.launchpad.net/~duplicity-team/duplicity/0.7-series/view/head:/duplicity/backends/azurebackend.py
#
# *********************************************************
#
# Copyright 2013 Matthieu Huin <mhu@enovance.com>
# Copyright 2015 Scott McKenzie <noizyland@gmail.com>
#
# This file is part of duplicity.
#
# Duplicity is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# Duplicity is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with duplicity; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

import os
import re
import time

from duplicity import globals

import duplicity.backend
from duplicity import log
from duplicity.errors import BackendException
import datetime

class AzureBackend(duplicity.backend.Backend):
    """
    Backend for Azure Blob Storage Service with support for tiering
    """
    def __init__(self, parsed_url):
        self.lastVol = None
        self.putHist = {}
        self.doClose = False
        # Leave these in the default Tier
        self.sizeLimit = 90*1024
        if hasattr(globals, 'az_size_limit'):
            self.sizeLimit = globals.az_size_limit
        duplicity.backend.Backend.__init__(self, parsed_url)

        self.isArchive = parsed_url.scheme.endswith('+archive')

        # Import Microsoft Azure Storage SDK for Python library.
        try:
            import azure
            import azure.storage
            import azure.storage.blob
            if hasattr(azure.storage.blob, 'BlockBlobService'):
                from azure.storage.blob import BlockBlobService
                self.AzureMissingResourceError = azure.common.AzureMissingResourceHttpError
                self.AzureConflictError = azure.common.AzureConflictHttpError
            else:
                raise BackendException("Unsupported Azure Storage SDK version");
        except ImportError as e:
            raise BackendException("""\
Azure backend requires Microsoft Azure Storage SDK for Python (https://pypi.python.org/pypi/azure-storage/).
Exception: %s""" % str(e))

        if 'AZURE_ACCOUNT_NAME' not in os.environ:
            raise BackendException('AZURE_ACCOUNT_NAME environment variable not set.')
        if 'AZURE_ACCOUNT_KEY' not in os.environ:
            raise BackendException('AZURE_ACCOUNT_KEY environment variable not set.')
        self.blob_service = BlockBlobService(account_name=os.environ['AZURE_ACCOUNT_NAME'],
                                        account_key=os.environ['AZURE_ACCOUNT_KEY'])

        # TODO: validate container name
        self.container = parsed_url.path.lstrip('/')
        try:
            self.blob_service.create_container(self.container, fail_on_exist=True)
        except self.AzureConflictError:
            # Indicates that the resource could not be created because it already exists.
            pass
        except Exception as e:
            log.FatalError("Could not create Azure container: %s"
                           % unicode(e.message).split('\n', 1)[0],
                           log.ErrorCode.connection_failed)

    def _isVol(self, name):
        return re.match("^duplicity-.*\.vol[0-9]*\.difftar\.gpg$", name)

    def _isManifest(self, name):
        return re.match("^duplicity-.*\.manifest.gpg$", name)

    def _isSignatures(self, name):
        return re.match("^duplicity-.*\.sigtar.gpg$", name)

    def _put(self, source_path, remote_filename):
        self.doClose = True
        self.putHist[remote_filename] = True

        # https://azure.microsoft.com/en-us/documentation/articles/storage-python-how-to-use-blob-storage/#upload-a-blob-into-a-container
        self.blob_service.create_blob_from_path(self.container, remote_filename, source_path.name)

        if self.isArchive and self._isVol(remote_filename):
            if self.lastVol is not None:
                # TODO proper source tier indication
                self._update(self.lastVol, "Hot", self.lastVolDate, self.lastVolSize)

            self.lastVol = remote_filename
            self.lastVolSize = source_path.getsize()
            self.lastVolDate = datetime.datetime.now()

        del self.putHist[remote_filename]


    def pre_process_download(self, blobs):
        if not self.isArchive:
            return

        self.doClose = True
        # Start rehydration where necessary
        generator = self.blob_service.list_blobs(self.container)
        for blob in generator:
            if blob.name in blobs:
                if blob.properties.blob_tier != "Archive":
                    blobs.remove(blob.name)
                elif not hasattr(blob.properties, "rehydration_status"):
                    self.blob_service.set_standard_blob_tier(self.container, blob, "Hot")

        if len(blobs) == 0:
            return

        # Wait for 4 hours for rehydration to complete
        time.sleep(4*3600)

        # Retry every 30 minutes
        while True:
            generator = self.blob_service.list_blobs(self.container)
            for blob in generator:
                if blob.name in blobs and blob.properties.blob_tier != "Archive":
                    blobs.remove(blob.name)

            if len(blobs) == 0:
                break;

            time.sleep(1800)

    def _get(self, remote_filename, local_path):
        # https://azure.microsoft.com/en-us/documentation/articles/storage-python-how-to-use-blob-storage/#download-blobs
        self.blob_service.get_blob_to_path(self.container, remote_filename, local_path.name)

    def _getTier(self, modified):
        if hasattr(globals, "az_keep_cool"):
            deadline = datetime.date.today() - datetime.timedelta(days=globals.az_keep_cool)

            if deadline < modified.date():
                return "Cool"

        return "Archive"

    def _update(self, blob, tier, modified, size):
        if size < self.sizeLimit:
            return

        newTier = self._getTier(modified)

        if self._isSignatures(blob) or self._isManifest(blob):
            coolMeta = hasattr(globals, "az_cool_meta") and globals.az_cool_meta
            if coolMeta and newTier == "Archive":
                newTier = "Cool"

        if newTier == tier or newTier is None:
            return

        self.blob_service.set_standard_blob_tier(self.container, blob, newTier)

    def _close(self):
        if not self.isArchive or not self.doClose:
            return

        generator = self.blob_service.list_blobs(self.container)
        for blob in generator:
            if blob in self.putHist:
                continue

            self._update(blob.name, blob.properties.blob_tier, blob.properties.last_modified, int(blob.properties.content_length))

    def _list(self):
        # https://azure.microsoft.com/en-us/documentation/articles/storage-python-how-to-use-blob-storage/#list-the-blobs-in-a-container
        blobs = []

        generator = self.blob_service.list_blobs(self.container)
        for blob in generator:
            blobs.append(blob.name)

        return blobs

    def _delete(self, filename):
        # http://azure.microsoft.com/en-us/documentation/articles/storage-python-how-to-use-blob-storage/#delete-blobs
        self.blob_service.delete_blob(self.container, filename)

    def _query(self, filename):
        blob = self.blob_service.get_blob_properties(self.container, filename)
        return {'size': int(blob.properties.content_length)}

    def _error_code(self, operation, e):
        if isinstance(e, self.AzureMissingResourceError):
            return log.ErrorCode.backend_not_found

duplicity.backend.register_backend('azure+archive', AzureBackend)
duplicity.backend.register_backend('azure', AzureBackend)
