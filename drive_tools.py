"""Google Drive tools สำหรับให้ Gemini เรียกใช้ (function calling)"""

import io
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT_FOLDER_ID = os.environ["DRIVE_ROOT_FOLDER_ID"]
CLIENT_SECRET_FILE = os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json")
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")

# บนคลาวด์: ส่ง token ผ่าน env var GOOGLE_TOKEN_JSON แทนไฟล์ (ไฟล์ไม่ถูก commit)
if not os.path.exists(TOKEN_FILE) and os.environ.get("GOOGLE_TOKEN_JSON"):
    with open(TOKEN_FILE, "w", encoding="utf-8") as _f:
        _f.write(os.environ["GOOGLE_TOKEN_JSON"])

_service = None


def _creds():
    """OAuth ในนามบัญชีผู้ใช้ (service account อัปโหลดลง My Drive ไม่ได้แล้ว)"""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def drive():
    global _service
    if _service is None:
        _service = build("drive", "v3", credentials=_creds())
    return _service


FILE_FIELDS = "id, name, mimeType, modifiedTime, size, parents, webViewLink"


def _fmt(files: list[dict]) -> str:
    if not files:
        return "ไม่พบไฟล์"
    return json.dumps(files, ensure_ascii=False, indent=1)


def search_files(query: str) -> str:
    """ค้นหาไฟล์/โฟลเดอร์ใน Google Drive จากชื่อ (ค้นแบบ contains)

    Args:
        query: คำค้นหาในชื่อไฟล์ เช่น "ราคาประเมิน" หรือ "น้ำพอง"
    """
    q = query.replace("'", "\\'")
    res = drive().files().list(
        q=f"name contains '{q}' and trashed=false",
        fields=f"files({FILE_FIELDS})",
        pageSize=20,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return _fmt(res.get("files", []))


def list_folder(folder_id: str = "") -> str:
    """แสดงรายการไฟล์ในโฟลเดอร์ (ถ้าไม่ระบุ folder_id จะใช้โฟลเดอร์หลักของบอท)

    Args:
        folder_id: ID ของโฟลเดอร์ที่ต้องการดู (เว้นว่าง = โฟลเดอร์หลัก)
    """
    fid = folder_id or ROOT_FOLDER_ID
    res = drive().files().list(
        q=f"'{fid}' in parents and trashed=false",
        fields=f"files({FILE_FIELDS})",
        pageSize=50,
        orderBy="folder,name",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return _fmt(res.get("files", []))


def create_folder(name: str, parent_id: str = "") -> str:
    """สร้างโฟลเดอร์ใหม่

    Args:
        name: ชื่อโฟลเดอร์
        parent_id: ID โฟลเดอร์แม่ (เว้นว่าง = สร้างในโฟลเดอร์หลักของบอท)
    """
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id or ROOT_FOLDER_ID],
    }
    f = drive().files().create(
        body=meta, fields=FILE_FIELDS, supportsAllDrives=True
    ).execute()
    return _fmt([f])


def move_file(file_id: str, new_parent_id: str) -> str:
    """ย้ายไฟล์/โฟลเดอร์ไปยังโฟลเดอร์ปลายทาง

    Args:
        file_id: ID ของไฟล์ที่จะย้าย
        new_parent_id: ID ของโฟลเดอร์ปลายทาง
    """
    f = drive().files().get(
        fileId=file_id, fields="parents", supportsAllDrives=True
    ).execute()
    prev = ",".join(f.get("parents", []))
    f = drive().files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=prev,
        fields=FILE_FIELDS,
        supportsAllDrives=True,
    ).execute()
    return _fmt([f])


def rename_file(file_id: str, new_name: str) -> str:
    """เปลี่ยนชื่อไฟล์/โฟลเดอร์

    Args:
        file_id: ID ของไฟล์
        new_name: ชื่อใหม่
    """
    f = drive().files().update(
        fileId=file_id, body={"name": new_name},
        fields=FILE_FIELDS, supportsAllDrives=True,
    ).execute()
    return _fmt([f])


def get_link(file_id: str) -> str:
    """ขอลิงก์เปิดดูไฟล์ (webViewLink)

    Args:
        file_id: ID ของไฟล์
    """
    f = drive().files().get(
        fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True
    ).execute()
    return _fmt([f])


def trash_file(file_id: str) -> str:
    """ย้ายไฟล์ไปถังขยะ (กู้คืนได้ใน 30 วัน) — เรียกใช้เฉพาะเมื่อผู้ใช้ยืนยันแล้วเท่านั้น

    Args:
        file_id: ID ของไฟล์ที่จะลบ
    """
    f = drive().files().update(
        fileId=file_id, body={"trashed": True},
        fields="id, name", supportsAllDrives=True,
    ).execute()
    return f"ย้ายไปถังขยะแล้ว: {f.get('name')} ({f.get('id')})"


ALL_TOOLS = [
    search_files,
    list_folder,
    create_folder,
    move_file,
    rename_file,
    get_link,
    trash_file,
]


def upload_bytes(data: bytes, filename: str, mime_type: str,
                 parent_id: str | None = None) -> dict:
    """อัปโหลดไฟล์ (ใช้จากฝั่ง webhook ตอนผู้ใช้ส่งไฟล์เข้ามาใน LINE)"""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    meta = {"name": filename, "parents": [parent_id or ROOT_FOLDER_ID]}
    return drive().files().create(
        body=meta, media_body=media, fields=FILE_FIELDS, supportsAllDrives=True
    ).execute()
