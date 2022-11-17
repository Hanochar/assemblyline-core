#!/usr/bin/env python
from assemblyline.datastore.collection import ESCollection
import elasticapm
import os
import tempfile

from assemblyline.common import forge
from assemblyline.common.metrics import MetricsFactory
from assemblyline.odm.messages.archive_heartbeat import Metrics
from assemblyline.odm.models.submission import Submission
from assemblyline.remote.datatypes import get_client
from assemblyline.remote.datatypes.queues.named import NamedQueue

from assemblyline_core.server_base import ServerBase

ARCHIVE_QUEUE_NAME = 'm-archive'


class SubmissionNotFound(Exception):
    pass


class WebhookFailed(Exception):
    pass


class Archiver(ServerBase):
    def __init__(self):
        super().__init__('assemblyline.archiver')
        # Publish counters to the metrics sink.
        self.counter = MetricsFactory('archiver', Metrics)
        self.datastore = forge.get_datastore(self.config, archive_access=True)
        self.filestore = forge.get_filestore(config=self.config)
        self.archivestore = forge.get_archivestore(config=self.config)
        self.persistent_redis = get_client(
            host=self.config.core.redis.persistent.host,
            port=self.config.core.redis.persistent.port,
            private=False,
        )

        self.archive_queue: NamedQueue[dict] = NamedQueue(ARCHIVE_QUEUE_NAME, self.persistent_redis)
        if self.config.core.metrics.apm_server.server_url is not None:
            self.log.info(f"Exporting application metrics to: {self.config.core.metrics.apm_server.server_url}")
            elasticapm.instrument()
            self.apm_client = elasticapm.Client(server_url=self.config.core.metrics.apm_server.server_url,
                                                service_name="alerter")
        else:
            self.apm_client = None

    def stop(self):
        if self.counter:
            self.counter.stop()

        if self.apm_client:
            elasticapm.uninstrument()
        super().stop()

    def run_once(self):
        message = self.archive_queue.pop(timeout=1)

        # If there is no alert bail out
        if not message:
            return
        else:
            try:
                archive_type, type_id, delete_after = message
                self.counter.increment('received')
            except Exception:
                self.log.error(f"Invalid message received: {message}")
                return

        # Start of process alert transaction
        if self.apm_client:
            self.apm_client.begin_transaction('Process archive message')

        try:
            if archive_type == "submission":
                self.counter.increment('submission')
                # Load submission
                submission: Submission = self.datastore.submission.get_from_archive(type_id)
                if not submission:
                    submission: Submission = self.datastore.submission.get_if_exists(
                        type_id, archive_access=False)
                    if not submission:
                        raise SubmissionNotFound(type_id)
                    # TODO:
                    #    Call / wait for webhook
                    #    Save it to the archive with extra metadata

                    # Reset Expiry
                    submission.expiry_ts = None
                    submission.archived = True
                    self.datastore.submission.save_to_archive(type_id, submission, delete_after=delete_after)
                    self.datastore.submission.update(type_id, [(ESCollection.UPDATE_SET, 'archived', True)])
                elif delete_after:
                    self.datastore.submission.delete(type_id, archive_access=False)

                # Gather list of files and archives them
                files = {f.sha256 for f in submission.files}
                files.update(self.datastore.get_file_list_from_keys(submission.results, supplementary=True))
                for sha256 in files:
                    self.counter.increment('file')
                    self.datastore.file.archive(sha256, delete_after=delete_after)
                    if self.filestore != self.archivestore:
                        with tempfile.NamedTemporaryFile() as buf:
                            self.filestore.download(sha256, buf.name)
                            try:
                                if os.path.getsize(buf.name):
                                    self.archivestore.upload(buf.name, sha256)
                            except Exception as e:
                                self.log.error(
                                    f"Could not copy file {sha256} from the filestore to the archivestore. ({e})")

                # Archive associated results (Skip emptys)
                for r in submission.results:
                    if not r.endswith(".e"):
                        self.counter.increment('result')
                        self.datastore.result.archive(r, delete_after=delete_after)

                # End of process alert transaction (success)
                self.log.info(f"Successfully archived submission '{type_id}'.")
                if self.apm_client:
                    self.apm_client.end_transaction(archive_type, 'success')

            # Invalid archiving type
            else:
                self.counter.increment('invalid')
                self.log.warning(f"'{archive_type}' is not a valid archive type.")
                # End of process alert transaction (success)
                if self.apm_client:
                    self.apm_client.end_transaction(archive_type, 'invalid')

        except SubmissionNotFound:
            self.counter.increment('not_found')
            self.log.warning(f"Could not archive {archive_type} '{type_id}'. It was not found in the system.")
            # End of process alert transaction (failure)
            if self.apm_client:
                self.apm_client.end_transaction(archive_type, 'not_found')

        except WebhookFailed as wf:
            self.counter.increment('webhook_failure')
            self.log.warning(f"Could not archive {archive_type} '{type_id}'. Webhook failed with error: {wf}")
            # End of process alert transaction (failure)
            if self.apm_client:
                self.apm_client.end_transaction(archive_type, 'webhook_failure')

        except Exception:  # pylint: disable=W0703
            self.counter.increment('exception')
            self.log.exception(f'Unhandled exception processing {archive_type} ID: {type_id}')

            # End of process alert transaction (failure)
            if self.apm_client:
                self.apm_client.end_transaction(archive_type, 'exception')

    def try_run(self):
        while self.running:
            self.heartbeat()
            self.run_once()


if __name__ == "__main__":
    with Archiver() as archiver:
        archiver.serve_forever()
