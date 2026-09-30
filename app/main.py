"""FastAPI 入口：启动时初始化数据库。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import router
from .database import init_db
from .errors import ApiError


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 一键启动并初始化数据库（幂等）
    init_db()
    yield


app = FastAPI(
    title="实验室测量可信性追溯服务",
    description=(
        "校准证书与测量读数的可信性追溯：证书签发/纠正/撤销、读数按提交版本匹配证书、"
        "单调版本、请求幂等与历史可查。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 无效区间、缺字段等参数错误：422，不产生任何变更
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "request validation failed",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


app.include_router(router)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "measurement-traceability", "docs": "/docs", "api": "/api/v1"}
