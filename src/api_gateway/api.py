from fastapi import FastAPI
from pydantic import BaseModel
from enum import Enum
import uuid
import asyncio

import queue_helper
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "queue_api"))
from queue_client import AbstractQueueProducer

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "db"))
from db_wrapper import AbstractDB, SummarizeJobLog

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "log"))
from custom_log import AbstractLogger


class ResponseInferenceStatusDetail(str, Enum):
    complete_job = "inference job is completed"
    in_progress_job = "inference job is progress now. please try to get result again after a few moments"
    job_is_not_found = (
        "inference job is not found. please try to request inference again"
    )
    complete_job_but_not_found_inference_result = "inference job is completed! but inference result is not found because of unknown error"


class ResponseInferenceStatusCode(int, Enum):
    complete_job = 0
    in_progress_job = 1
    job_is_not_found = 2
    complete_job_but_not_found_inference_result = 3


class ResponseInferenceStatus(Enum):
    complete_job = (
        ResponseInferenceStatusCode.complete_job,
        ResponseInferenceStatusDetail.complete_job,
    )
    in_progress_job = (
        ResponseInferenceStatusCode.in_progress_job,
        ResponseInferenceStatusDetail.in_progress_job,
    )
    job_is_not_found = (
        ResponseInferenceStatusCode.job_is_not_found,
        ResponseInferenceStatusDetail.job_is_not_found,
    )
    complete_job_but_not_found_inference_result = (
        ResponseInferenceStatusCode.complete_job_but_not_found_inference_result,
        ResponseInferenceStatusDetail.complete_job_but_not_found_inference_result,
    )

    def get_id(self) -> int:
        return self.value[0]

    def get_detail(self) -> str:
        return self.value[1]


class ResponseSetCorrectedResult(Enum):
    complete_job = (0, "set corrected label job is completed!")
    inference_job_is_not_completed = (1, "")
    inference_job_is_not_found = (
        2,
        "inference job is not found. please try to request inference again.",
    )
    complete_inference_job_but_not_found_inference_result = (
        3,
        "inference job is completed! but inference result is not found because of unknown error.",
    )
    failed_to_update_label = (
        4,
        "fail to update corrected text because of db error",
    )

    def get_id(self) -> int:
        return self.value[0]

    def get_detail(self) -> str:
        return self.value[1]


class InputData(BaseModel):
    body: str


class ResponseData(BaseModel):
    job_id: str
    status_code: ResponseInferenceStatusCode
    status_detail: ResponseInferenceStatusDetail
    predicted_text: str


# TODO : enum??????????????????????????????????????????response??????????????????????????????
class CorrectInputData(BaseModel):
    job_id: str
    corrected_text: str


class SummarizerApi:
    def __init__(
        self,
        queue_producer: AbstractQueueProducer,
        db_instance: AbstractDB,
        logger: AbstractLogger,
    ) -> None:
        self.__queue_producer = queue_producer
        self.__db_instance = db_instance
        self.__logger = logger

    def create_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/request_summarize/")
        async def request_summarize(input_data: InputData):
            return await self.__request_summarize(input_data=input_data)

        @app.get("/summarize_result/", response_model=ResponseData)
        async def get_summarize_result(job_id: str):
            return await self.__get_summarize_result(job_id=job_id)

        @app.post("/set_correct_summarize_result/")
        async def set_correct_summarize_result(correct_data: CorrectInputData):
            return await self.__set_correct_summarize_result(
                correct_data=correct_data
            )

        return app

    async def __request_summarize(self, input_data: InputData):
        # TODO : ???????????????????????????
        # ???????????????????????????loop???????????????????????????????????????????????????nest?????????????????????????????????
        loop = asyncio.get_running_loop()
        id = await queue_helper.send_body_into_queue_task(
            loop=loop,
            body_text=input_data.body,
            producer=self.__queue_producer,
        )

        # job?????????DB?????????
        job_log = SummarizeJobLog(job_id=uuid.UUID(id))
        self.__db_instance.insert_summarize_job_log(job_logs=[job_log])
        return {"message_id": id}

    async def __get_summarize_result(self, job_id: str):
        job_id = uuid.UUID(job_id)

        def make_response(
            response_status: ResponseInferenceStatus, predicted_text: str = ""
        ) -> ResponseData:
            d = {
                "job_id": str(job_id),
                "status_code": response_status.get_id(),
                "status_detail": response_status.get_detail(),
                "predicted_text": predicted_text,
            }
            return ResponseData(**d)

        # ????????????????????????????????????
        job_log = self.__db_instance.fetch_summarize_job_log_by_id(
            job_id=job_id
        )
        if job_log is None:
            response_status = ResponseInferenceStatus.job_is_not_found
            return make_response(response_status=response_status)

        # ???????????????????????????????????????(SummarizeJobLog??????????????????????????????SummarizeJobInfo?????????????????????????????????)
        job_info = self.__db_instance.fetch_summarize_job_info(job_id=job_id)
        if job_log is not None and job_info is None:
            response_status = ResponseInferenceStatus.in_progress_job
            return make_response(response_status=response_status)

        # ????????????????????????????????????
        result = self.__db_instance.fetch_summarize_result_by_id(
            result_id=job_info.result_id
        )
        if result is None:
            # ????????????????????????????????????????????????????????????????????????????????????????????????????????????
            response_status = (
                ResponseInferenceStatus.complete_job_but_not_found_inference_result
            )
            return make_response(response_status=response_status)
        return make_response(
            response_status=ResponseInferenceStatus.complete_job,
            predicted_text=result.get_predicted_text(),
        )

    # TODO : update???????????????????????????????????????????????????????????????????????????????????????
    async def __set_correct_summarize_result(
        self, correct_data: CorrectInputData
    ):
        job_id = uuid.UUID(correct_data.job_id)
        job_info = self.__db_instance.fetch_summarize_job_info(job_id=job_id)

        def make_response(
            response_status: ResponseSetCorrectedResult,
            result_detail: dict = {},
        ) -> dict:
            return {
                "status_code": response_status.get_id(),
                "status_detail": response_status.get_detail(),
                "result_detail": result_detail,
            }

        # ????????????????????????????????????
        if job_info is None:
            response_status = (
                ResponseSetCorrectedResult.inference_job_is_not_found
            )
            return make_response(response_status=response_status)

        # ???????????????????????????????????????
        if job_info.result_id is None:
            response_status = (
                ResponseSetCorrectedResult.inference_job_is_not_completed
            )
            return make_response(response_status=response_status)

        # ????????????????????????????????????
        try:
            self.__db_instance.update_label_text_of_result_by_id(
                result_id=job_info.result_id,
                label_text=correct_data.corrected_text,
            )
        except Exception as e:
            self.__logger.error(
                "update corrected text! error detail is {}".format(e)
            )
            response_status = ResponseSetCorrectedResult.failed_to_update_label
            return make_response(response_status=response_status)
        return make_response(
            response_status=ResponseSetCorrectedResult.complete_job
        )
