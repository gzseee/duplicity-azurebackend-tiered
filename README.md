# Duplicity Azure Archive Backend

DO NOT MAKE ME RESPONSIBLE FOR UNEXPECTED COSTS. USE AT YOUR OWN RISK. DO NOT USE IF YOU DON'T KNOW WHAT YOU ARE DOING.

This is modified experimental version of Azure Backend from duplicity 0.7.16. Unlike the original one, it is trying to take advantage of tiering to save on storage expenses.

If you will decide to use it, make sure you will do throughout testing.

## Install

First install Azure SDK and login. This backend was tested with version 0.36.0.

Then prepare a storage account and note the access key. I would recommend

  * Account kind: StorageV2
  * Replication: LRS
  * Access tier: Hot
  * Secure transfer required: Enabled

Afterwards prepare duplicity sources. Following instructions were tested on Ubuntu 17.04
and assume you don't have duplicity installed from packages.

```
git clone https://github.com/gzseee/duplicity-azurebackend-tiered.git

cd duplicity-azurebackend-tiered
wget https://launchpad.net/duplicity/0.7-series/0.7.16/+download/duplicity-0.7.16.tar.gz
tar xvzf duplicity-0.7.16.tar.gz
cd duplicity-0.7.16

# Add command line options
sed -i "s/^\(.*s3-use-server-side-encryption.*\)$/\1\n    parser.add_option(\"--az-keep-cool\", type=\"int\")\n    parser.add_option(\"--az-size-limit\", type=\"int\")\n    parser.add_option(\"--az-cool-meta\", action=\"store_true\")\n/" duplicity/commandline.py

# Fix pre_process_download
sed -i "s/hasattr(globals\.backend, 'pre_process_download')/hasattr(globals.backend.backend, 'pre_process_download')/;s/globals\.backend\.pre_process_download/globals.backend.backend.pre_process_download/" bin/duplicity

# Add backend
cp ../azurebackend.py duplicity/backends

# Install
sudo apt install librsync-dev
sudo python2.7 setup.py install
```

## Use

Below are usage examples.

```
export AZURE_ACCOUNT_NAME=__account_name__
export AZURE_ACCOUNT_KEY=__account_key__
export PASSPHRASE=__passphrase__

duplicity /backup-folder azure+archive://container
duplicity cleanup --force azure+archive://container
duplicity collection-status azure+archive://container
duplicity remove-older-than 6M azure+archive://container

# This will take some time because file will have to be rehydrated first
duplicity restore --file-to-restore file azure+archive://container /restore-folder
```

## Supported arguments

There are some custom arguments for this backend.

  * --az-keep-cool 30

This will move files to Cool for first 30 days and on next container action
(like another backup) to Archive.

  * --az-size-limit 92160

This will ensure that files below 90 kB (default) are not moved from the inferred tier.
Keeping such files in Hot rather then moving anywhere else should be worthy if
kept for about 6 months.

  * --az-cool-meta

By default, all large enough files are moved to Archive tier as soon as possible.
With this option, sigtars and manifests will be stored on Cool instead of Archive.
This allows cheaper and faster recovery after loss of local duplicity cache.

## Notes

There are some things to consider

  * Files are moved also on backend close, so it is necessary to pass arguments
    above to every issued command.

  * By default, files rehydrated for restore are moved to Hot and moved back to
    Archive tier right after all of them are downloaded.

  * Archive and Cool tier have early deletion costs.

  * Restore can be very expansive. Especially from the Archive tier.
