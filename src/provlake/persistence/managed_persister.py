import traceback
from requests_futures.sessions import FuturesSession
from time import sleep
from urllib.parse import urljoin
import json
from requests.exceptions import ConnectionError
import urllib3
from typing import List
from provlake.persistence.persister import Persister
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from provlake.model.activity_prov_obj import ProvRequestObj
import logging

offline_prov_log = logging.getLogger("OFFLINE_PROV")
logger = logging.getLogger('PROV')


class ManagedPersister(Persister):

    def __init__(self, workflow_name: str, wf_start_time: float, service_url: str, wf_exec_id=None, context: str = None,
                 with_validation: bool = False, db_name: str = None, bag_size: int = 1,
                 should_send_to_file: bool = False, should_send_to_service: bool = True):
        super().__init__(workflow_name, wf_start_time, wf_exec_id)
        self.retrospective_url = urljoin(service_url, "retrospective-provenance")
        self.prospective_url = urljoin(service_url, "prospective-provenance")
        self.context = context
        self.with_validation = with_validation
        self.db_name = db_name
        self.requests_queue = list()
        self.bag_size = bag_size
        self.should_send_to_service = should_send_to_service
        self.should_send_to_file = should_send_to_file

        self.session = None
        if self.should_send_to_service:
            logger.debug("You are using the Service URL: " + service_url)
            self.session = FuturesSession()

    def add_request(self, persistence_request: ProvRequestObj):
        try:
            request_data = persistence_request.as_dict()
            if self.context:
                request_data["context"] = self.context
            self.requests_queue.append(request_data)
            if len(self.requests_queue) >= self.bag_size:
                self._flush()
            # if `configuration` is present this object should be persisted synchronously
            # if "configuration" in prov_obj:
            #     self.__flush__(True)
        except Exception:
            logger.error("[Prov] Unexpected exception")
            traceback.print_exc()
            pass

    def _close(self):
        if self.session:
            logger.info("Waiting to get response from all submitted provenance tasks...")
            while not self.session.executor._work_queue.empty():
                # wait to guarantee that all provenance requests have been sent (fired) to collector service
                sleep(0.1)
        # Persist remaining tasks synchronously
        self._flush(all_and_wait=True)
        if self.session:
            self.session.close()

    def _flush(self, all_and_wait: bool = False):
            if len(self.requests_queue) > 0:
                if all_and_wait:
                    logger.debug("Going to flush everything. Flushing " + str(len(self.requests_queue)))
                    if self.should_send_to_file:
                        offline_prov_log.debug(json.dumps(self.requests_queue))
                    if self.should_send_to_service:
                        self._send_to_service(self.requests_queue)
                    self.requests_queue = list()
                else:
                    to_flush = self.requests_queue[:self.bag_size]
                    del self.requests_queue[:self.bag_size]
                    logger.debug("Going to flush a part. Flushing " + str(len(to_flush)) + " out of " +
                                str(len(self.requests_queue)))
                    if self.should_send_to_file:
                        offline_prov_log.debug(json.dumps(to_flush))
                    if self.should_send_to_service:
                        self._send_to_service(to_flush)

    def _send_to_service(self, to_flush: List[dict]):
        params = {"with_validation": str(self.with_validation), "db_name": self.db_name}
        try:
            logger.debug("[Prov-Persistence]" + json.dumps(to_flush))
            # TODO: check whether we need this result() below
            r = self.session.post(self.retrospective_url, json=to_flush, params=params, verify=False).result()
        except ConnectionError as ex:
            logger.error(
                "[Prov][ConnectionError] There is a communication error between client and server -> " + str(ex))
            r = None
            pass
        except Exception as ex:
            traceback.print_exc()
            logger.error(
                "[Prov] Unexpected exception while adding retrospective provenance: " + type(ex).__name__
                + "->" + str(ex))
            r = None
            pass
        # If requests were validated, check for errors
        if r and self.with_validation:
            self._log_validation_message(r)

    def persist_prospective(self, json_data: dict):
        try:
            if self.should_send_to_file:
                offline_prov_log.debug(json.dumps(self.requests_queue))
            if self.should_send_to_service:
                logger.debug("[Prov-Persistence][Prospective]" + json.dumps(json_data))
                try:
                    r = self.session.post(self.prospective_url, json=json_data, params={'overwrite': True},
                                          verify=False).result()
                    if 200 <= r.status_code <= 209:
                        logger.debug("Prospective provenance inserted successfully.")
                    elif r.status_code == 406:
                        error_parsed = json.loads(r._content.decode('utf-8'))
                        error_obj = error_parsed['error'].replace("'", '"')
                        logger.error(error_obj)
                    elif r.status_code == 500:
                        r = self.session.put(self.prospective_url, json=json_data).result()
                        try:
                            assert 200 <= r.status_code <= 209
                        except AssertionError:
                            logger.error("Prospective provenance was not inserted correctly. Status code = " + str(r.status_code))
                    elif r.status_code > 300:
                        logger.error("Prospective provenance was not inserted correctly. Status code = " + str(r.status_code))
                except ConnectionError as ex:
                    traceback.print_exc()
                    logger.error("[Prov][ConnectionError] There is a communication error between client and server -> " + str(
                            ex))
                    pass
                except Exception as ex:
                    logger.error("[Prov] Unexpected exception while adding prospective provenance: " + type(ex).__name__)
                    pass
        except Exception as ex:
            logger.error("[Prov] Unexpected exception " + type(ex).__name__)
            traceback.print_exc()
            pass

    @staticmethod
    def _log_validation_message(response):
        error_obj = json.loads(response._content.decode('utf-8'))
        if len(error_obj['error']) > 0:
            for error_list in error_obj['error']:
                for error in error_list:
                    if error['code'][0] == 'W':
                        logger.warning('{} {}{}'.format(error['type'], error['explanation'], '\n'))
                    else:
                        logger.error('{} {}{}'.format(error['type'], error['explanation'], '\n'))
