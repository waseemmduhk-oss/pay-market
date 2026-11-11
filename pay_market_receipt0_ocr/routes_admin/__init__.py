from .common import admin_bp

# استيراد كل الوحدات الفرعية ضروري هنا حتى تُنفَّذ الديكوريترات (@admin_bp.route)
# التي تسجّل المسارات على admin_bp — الاستيراد نفسه هو ما يُفعّلها.
from . import auth        # noqa: F401  (تسجيل الدخول/الخروج، heartbeat الجلسة)
from . import vouchers    # noqa: F401  (اللوحة الرئيسية، رفع/حذف/تصدير البطاقات)
from . import payments    # noqa: F401  (إدارة الدفعات، تيليجرام، مراجعة إيصالات OCR)

__all__ = ['admin_bp']
