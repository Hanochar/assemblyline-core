
import concurrent.futures
import time

from al_core.server_base import ServerBase
from assemblyline.common import forge, net
from assemblyline.filestore import FileStore
from assemblyline.remote.datatypes.exporting_counter import AutoExportingCounters

config = forge.get_config()


class ExpiryManager(ServerBase):
    def __init__(self):
        super().__init__('assemblyline.expiry', shutdown_timeout=config.core.expiry.sleep_time + 5)
        self.datastore = forge.get_datastore()
        self.filestore = forge.get_filestore()
        self.cachestore = FileStore(*config.filestore.cache)
        self.expirable_collections = []
        self.counter = AutoExportingCounters(
            name='expiry',
            host=net.get_hostip(),
            export_interval_secs=5,
            channel=forge.get_metrics_sink(),
            auto_log=False,
            auto_flush=True)
        self.counter.start()

        self.fs_hashmap = {
            'file': self.filestore.delete,
            'cached_file': self.cachestore.delete
        }

        for name, definition in self.datastore.ds.get_models().items():
            if hasattr(definition, 'expiry_ts'):
                self.expirable_collections.append(getattr(self.datastore, name))

    def close(self):
        if self.counter:
            self.counter.stop()

    def try_run(self):
        while self.running:
            for collection in self.expirable_collections:
                if config.core.expiry.batch_delete:
                    delete_query = f"expiry_ts:[* TO {self.datastore.ds.now}-{config.core.expiry.delay}" \
                        f"{self.datastore.ds.hour}/DAY]"
                else:
                    delete_query = f"expiry_ts:[* TO {self.datastore.ds.now}-{config.core.expiry.delay}" \
                        f"{self.datastore.ds.hour}]"

                number_to_delete = collection.search(delete_query, rows=0, as_obj=False)['total']

                self.log.info(f"Processing collection: {collection.name}")
                if number_to_delete != 0:
                    if config.core.expiry.delete_storage and collection.name in self.fs_hashmap:
                        # Delete associated files
                        with concurrent.futures.ThreadPoolExecutor(config.core.expiry.workers) as executor:
                            res = {item['id']: executor.submit(self.fs_hashmap[collection.name], item['id'])
                                   for item in collection.stream_search(delete_query, fl='id', as_obj=False)}
                        for v in res.values():
                            v.result()
                        self.log.info(f'    Deleted associated files from the '
                                      f'{"cachestore" if "cache" in collection.name else "filestore"}...')

                    # Proceed with deletion
                    collection.delete_matching(delete_query, workers=config.core.expiry.workers)
                    self.counter.increment(f'expiry.{collection.name}', increment_by=number_to_delete)

                    self.log.info(f"    Deleted {number_to_delete} items from the datastore...")
                else:
                    self.log.debug("    Nothing to delete in this collection.")

            time.sleep(config.core.expiry.sleep_time)


if __name__ == "__main__":
    with ExpiryManager() as em:
        em.serve_forever()
