"""
文件服务器上传通用类
流程：
  1. POST verify-server/token/generate 用 appId + appSecurity 换取 accessToken
  2. POST fileview.../dfs/{group}/upload 带 accessToken 头上传本地文件
依赖：requests
"""
from __future__ import annotations

import os
import logging
import requests
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class FileServerUploader:
    def __init__(
        self,
        app_id: Optional[str] = None,
        app_security: Optional[str] = None,
        token_url: str = "https://www.ynruolin.net/verify-server/token/generate",
        upload_url: str = "https://fileview.ynruolin.net/dfs/group1/upload",
    ):
        load_dotenv()
        self.app_id = app_id or os.getenv("FILE_SERVER_APP_ID", "")
        self.app_security = app_security or os.getenv("FILE_SERVER_APP_SECURITY", "")
        if not self.app_id or not self.app_security:
            raise ValueError("缺少 appId / appSecurity（请传参或在 .env 配置）")

        self.token_url = os.getenv("FILE_SERVER_TOKEN_URL", token_url)
        self.upload_url = os.getenv("FILE_SERVER_UPLOAD_URL", upload_url)
        self._token: Optional[str] = None

    # ---------- 取 token ----------
    def get_token(self, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token

        # --form 对应 multipart/form-data
        resp = requests.post(
            self.token_url,
            files={
                "appId": (None, self.app_id),
                "appSecurity": (None, self.app_security),
            },
            timeout=30,
        )
        resp.raise_for_status()

        body = resp.json()
        if body.get("code", 0) != 0:                      # 业务失败（如 code=1 无效企业）
            raise ValueError(f"获取 token 失败: {body.get('msg')} (code={body.get('code')})")

        token = (body.get("data") or {}).get("accessToken")
        if not token:
            raise ValueError(f"token 响应缺少 data.accessToken: {body}")
        self._token = token
        logger.info("已获取文件服务器 accessToken")
        return self._token

    # ---------- 上传 ----------
    def upload(self, local_path: str, output: str = "json") -> dict:
        if not os.path.isfile(local_path):
            raise FileNotFoundError(local_path)

        token = self.get_token()
        url = f"{self.upload_url}?output={output}&appId={self.app_id}"

        with open(local_path, "rb") as f:
            resp = requests.post(
                url,
                headers={"accessToken": token},
                files={
                    "file": (os.path.basename(local_path), f, "application/octet-stream"),
                },
                timeout=120,
            )
        resp.raise_for_status()

        body = resp.json()
        # 失败态：status=="fail" 或 retcode!=0
        if body.get("status") == "fail" or body.get("retcode", 0) != 0:
            raise RuntimeError(f"上传失败: {body.get('message') or body.get('retmsg') or body}")

        logger.info("上传成功: %s", body.get("url"))
        return body                                      # 调用方用 result["url"] 取下载地址

    # ---------- 上下文管理 ----------
    def __enter__(self) -> "FileServerUploader":
        self.get_token()
        return self

    def __exit__(self, *exc) -> None:
        self._token = None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python file_uploader.py <本地文件路径>")
        raise SystemExit(1)
    uploader = FileServerUploader()          # 从 .env 读取 appId / appSecurity
    result = uploader.upload(sys.argv[1])
    print("下载地址:", result.get("url"))
