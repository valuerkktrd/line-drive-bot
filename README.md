# LINE × Google Drive Bot (Gemini)

แชทบอทใน LINE — พิมพ์สั่งงาน แล้วบอทจัดการไฟล์ใน Google Drive ให้
ขับเคลื่อนด้วย `gemini-3.5-flash` (free tier) + function calling
Drive เข้าถึงผ่าน OAuth ในนามบัญชีผู้ใช้ (token.json) — จำกัดขอบเขตที่โฟลเดอร์ `LINE-Drive-Bot`

> หมายเหตุ free tier: Google อาจใช้ข้อความที่ส่งเข้า API ในการพัฒนาโมเดล — อย่าส่งข้อมูลลับผ่านบอท

## ความสามารถ

- ค้นหาไฟล์/โฟลเดอร์จากชื่อ
- ดูรายการไฟล์ในโฟลเดอร์
- สร้างโฟลเดอร์ / ย้าย / เปลี่ยนชื่อ
- ขอลิงก์เปิดไฟล์
- ลบ (ย้ายลงถังขยะ — บอทถามยืนยันก่อนเสมอ)
- ส่งไฟล์/รูปเข้าแชท → บอทอัปโหลดขึ้น Drive ให้อัตโนมัติ
- `/reset` หรือ "เริ่มใหม่" — ล้างประวัติสนทนา

## ติดตั้ง

### 1. LINE Developers

1. สร้าง Provider + Messaging API channel ที่ https://developers.line.biz
2. เก็บ **Channel secret** และออก **Channel access token** (long-lived)
3. เปิด "Use webhook" — ค่อยใส่ URL หลัง deploy (ข้อ 4)

### 2. Google Cloud

1. สร้าง project → เปิดใช้ **Google Drive API**
2. สร้าง **Service Account** → สร้าง key แบบ JSON → เซฟเป็น `service_account.json` ไว้ในโฟลเดอร์นี้
3. ใน Google Drive: สร้าง/เลือกโฟลเดอร์ที่จะให้บอทใช้ → **Share** ให้อีเมลของ service account (สิทธิ์ Editor)
4. เอา folder ID จาก URL (`https://drive.google.com/drive/folders/<ID ตรงนี้>`)

### 3. รันเครื่องตัวเอง (ทดสอบ)

```
cd line-drive-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # แล้วกรอกค่าจริง
python app.py
```

เปิด tunnel ให้ LINE ยิงเข้ามาได้ เช่น

```
ngrok http 8000
```

แล้วเอา URL ไปใส่ใน LINE console: `https://xxxx.ngrok.app/callback` → กด Verify

### 4. Deploy จริง

ที่ไหนก็ได้ที่มี HTTPS — Cloud Run / Railway / Render / VPS
ตั้ง env vars ตาม `.env.example` แล้วชี้ webhook มาที่ `/callback`

## หมายเหตุ

- **Push message quota**: บอทตอบรับด้วย reply message ก่อน แล้วส่งผลจริงด้วย push message
  (LINE OA แพลนฟรีจำกัด push ~200 ข้อความ/เดือน — ถ้าใช้เยอะให้อัปแพลน)
- **ประวัติสนทนา** เก็บใน memory — restart แล้วหาย ใช้งานจริงจังค่อยย้ายไป redis/db
- **ขอบเขตสิทธิ์**: service account เห็นเฉพาะโฟลเดอร์ที่ถูกแชร์ให้เท่านั้น ปลอดภัยโดยดีไซน์
- **ค่าใช้จ่าย**: Fable 5 = $10/$50 ต่อ 1M token (แพงกว่า Opus) — ถ้าอยากประหยัด
  เปลี่ยน `MODEL` ใน `bot.py` เป็น `claude-opus-4-8` ได้เลย (โค้ดที่เหลือใช้ได้เหมือนเดิม
  แต่ให้ลบ `betas`/`fallbacks` ออก)
- **Fable 5 ต้องการ data retention ≥ 30 วัน** — org ที่ตั้ง Zero Data Retention จะเรียกใช้ไม่ได้ (จะเจอ 400)

## โครงไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `app.py` | Flask webhook server — รับ event จาก LINE, ตอบกลับ |
| `bot.py` | เรียก Claude (tool runner) + จัดการประวัติสนทนา |
| `drive_tools.py` | Google Drive tools ที่ Claude เรียกใช้ได้ |
