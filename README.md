# استعلام اینماد

ابزار خط فرمان و کتابخانهٔ پایتون برای استعلام [نماد اعتماد الکترونیکی](https://enamad.ir/) یک یا چند دامنه.

از صفحهٔ اصلی enamad.ir جست‌وجو می‌کند، شناسه و کد اینماد را می‌گیرد، و در صورت در دسترس بودن صفحهٔ پروفایل، اطلاعات تماس و رتبه را هم برمی‌گرداند.

> **توجه:** سایت enamad.ir معمولاً فقط از IP داخل ایران در دسترس است.

## امکانات

- استعلام تکی یا دسته‌ای دامنه‌ها
- نوار پیشرفت زنده هنگام اجرا
- خروجی JSON و CSV (فایل بعد از هر دامنه به‌روز می‌شود)
- ادامه از فایل ناتمام با `--resume`
- تلاش مجدد برای خطای شبکه و فاصله بین درخواست‌ها
- تمایز وضعیت `ok` / `missing` / `error`
- خواندن فهرست دامنه از فایل
- اجرای مستقیم بدون `activate` کردن محیط مجازی
- قابل استفاده به‌صورت ماژول پایتون

## نصب

پایتون ۳.۱۰ یا جدیدتر لازم است.

```bash
git clone https://github.com/mohammadz/enamad-scraper.git
cd enamad-scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

پس از نصب، اسکریپت خودش مفسر `.venv` را پیدا می‌کند و نیازی به `source .venv/bin/activate` نیست.

## اجرا

```bash
python3 enamad.py example.ir
./enamad.py example.ir
```

### چند دامنه

```bash
python3 enamad.py example.ir shop.ir
python3 enamad.py example.ir,shop.ir
python3 enamad.py -f domains.txt
```

فایل `domains.txt` می‌تواند هر خط یک دامنه باشد. خط‌های خالی و توضیح با `#` نادیده گرفته می‌شوند.

### خروجی JSON

پیش‌فرض خروجی JSON است. یک دامنه به‌صورت یک آبجکت چاپ می‌شود؛ چند دامنه به‌صورت آرایه.

```bash
python3 enamad.py example.ir --json
python3 enamad.py example.ir shop.ir --json result.json
python3 enamad.py -f domains.txt --json result.json
```

اگر خروجی را در فایل ذخیره کنید، بعد از استعلام هر دامنه همان فایل به‌روز می‌شود.

### خروجی CSV

```bash
python3 enamad.py example.ir shop.ir --csv
python3 enamad.py -f domains.txt --csv result.csv
python3 enamad.py -f domains.txt -o result.csv
```

### ادامه پس از توقف

با `Ctrl+C` اجرا بدون traceback متوقف می‌شود و ردیف‌های نوشته‌شده در فایل می‌مانند. برای ادامه:

```bash
python3 enamad.py -f domains.txt --csv result.csv --resume
python3 enamad.py -f domains.txt --json result.json --resume
```

دامنه‌هایی که از قبل در فایل هستند رد می‌شوند.

### شبکه

```bash
python3 enamad.py -f domains.txt --csv result.csv --retries 3 --delay 0.5
```

`--retries` تعداد تلاش مجدد برای خطای شبکه است (پیش‌فرض ۲). `--delay` فاصله بین دامنه‌ها به ثانیه است (پیش‌فرض ۰٫۴).

### فلگ‌های دیگر

| فلگ | توضیح |
| --- | --- |
| `--check` | فقط وجود اینماد (`true` / `false`) |
| `--expired` | فقط منقضی بودن اینماد |
| `--debug` | جزئیات جست‌وجو در stderr |
| `-f` / `--file` | خواندن دامنه‌ها از فایل |
| `--json [FILE]` | خروجی JSON |
| `--csv [FILE]` | خروجی CSV |
| `-o` / `--output` | نوشتن خروجی در فایل |
| `--resume` | ادامه از فایل خروجی موجود |
| `--retries N` | تلاش مجدد برای خطای شبکه |
| `--delay SEC` | فاصله بین استعلام دامنه‌ها |

نمونه‌ها:

```bash
python3 enamad.py example.ir --check
python3 enamad.py example.ir --expired
python3 enamad.py example.ir --debug
```

دامنه می‌تواند به‌صورت `example.ir` یا `https://www.example.ir/path` باشد.

## فیلدهای خروجی

| کلید | نوع | توضیح |
| --- | --- | --- |
| `domain` | string | دامنهٔ نرمال‌شده |
| `status` | string | `ok`، `missing` یا `error` |
| `id` | int | شناسهٔ اینماد |
| `title` | string | عنوان کسب‌وکار |
| `name` | string | نام صاحب امتیاز |
| `start_date` | string | تاریخ اعطا (شمسی) |
| `expire_date` | string | تاریخ اعتبار (شمسی) |
| `address` | string | نشانی |
| `phone` | string | تلفن |
| `email` | string | رایانامه |
| `work_time` | string | ساعت پاسخگویی |
| `history` | string | سابقهٔ فعالیت |
| `star` | int | تعداد ستاره |
| `error` | string | پیام خطا؛ فقط وقتی `status` برابر `error` است |

- `ok`: اینماد پیدا شد
- `missing`: اینماد ندارد
- `error`: اتصال یا استعلام ناموفق بود

اگر دامنه اینماد نداشته باشد، متد `get()` مقدار `None` برمی‌گرداند.

نمونهٔ JSON:

```json
{
  "domain": "example.ir",
  "status": "ok",
  "id": 12345,
  "title": "فروشگاه نمونه",
  "name": "علی محمدی",
  "start_date": "1403/09/29",
  "expire_date": "1405/09/28",
  "address": "تهران",
  "phone": "02112345678",
  "email": "info@example.ir",
  "work_time": "09:00 الی 20:00",
  "history": "1 سال",
  "star": 1,
  "error": null
}
```

## استفاده در پایتون

```python
from enamad import Enamad, lookup_many

client = Enamad("example.ir")
client.has_enamad()  # bool
client.get()         # dict | None
client.is_expired()  # bool | None

lookup_many(["example.ir", "shop.ir"])
```

## تست

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## مجوز

MIT. متن کامل در فایل `LICENSE` است.
