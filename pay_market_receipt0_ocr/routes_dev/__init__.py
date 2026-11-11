from .common import dev_bp

# استيراد كل الوحدات الفرعية ضروري هنا حتى تُنفَّذ الديكوريترات (@dev_bp.route)
# التي تسجّل المسارات على dev_bp — الاستيراد نفسه هو ما يُفعّلها.
from . import auth        # noqa: F401  (تسجيل الدخول/الخروج، تغيير كلمات المرور)
from . import dashboard    # noqa: F401  (الصفحة الرئيسية، التقرير الشهري، صحة النظام)
from . import operators    # noqa: F401  (إدارة المشغّلين)
from . import logs         # noqa: F401  (سجل الأمان وسجل النشاط)
from . import backup       # noqa: F401  (النسخ الاحتياطي)

__all__ = ['dev_bp']
