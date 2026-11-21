import json
import os
import re
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger, Text, update, select, text as sql_text
from aristarkh_core.config import logger # Добавили логгер для отслеживания ошибок JSON

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    credits = Column(Integer, default=2) # [FIX] 2 бесплатных разбора на старте: 1 авто-диагноз после брифа + 1 боевой тест текста
    expires_at = Column(DateTime, nullable=True) # [FIX] Дата сгорания подписки
    temperature = Column(Float, default=0.7)
    created_at = Column(DateTime, default=datetime.utcnow)
    # [FIX] Поле для хранения срока действия безлимита
    unlimited_until = Column(DateTime, nullable=True)
    
    # === [STATE TRACKER: АФФЕКТИВНАЯ ПАМЯТЬ И МУЛЬТИПРОЕКТНОСТЬ] ===
    trust_level = Column(Integer, default=50) # От 0 (ненависть) до 100 (бро)
    fatigue = Column(Integer, default=0) # От 0 до 100, растет в сессии
    mood = Column(String, default="neutral") # Текущее настроение: neutral, sarcastic, angry, bored
    max_projects = Column(Integer, default=2) # Лимит папок проектов: 2 (Демо/Триал 1490), 3 (Эксперт 4900), 10 (Продюсер 9900)
    active_project_id = Column(Integer, nullable=True)
    
class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    amount = Column(Integer)     # Количество звезд (XTR)
    credits_added = Column(Integer) # Сколько кредитов начислено
    description = Column(String)
    telegram_charge_id = Column(String, unique=True) # Уникальный ID транзакции от Telegram
    date = Column(DateTime, default=datetime.utcnow)

# [FIX] Сквозная аналитика
class AnalyticsEvent(Base):
    __tablename__ = 'analytics'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    event_type = Column(String) # Например: 'START', 'MESSAGE_TEXT', 'MESSAGE_PHOTO', 'BUY_STARS'
    event_data = Column(String, nullable=True) # Доп инфа (напр. 'docx_upload', 'temp_change_0.7')
    timestamp = Column(DateTime, default=datetime.utcnow)

# Бриф проекта: 5 ответов пользователя, которые подмешиваются в контекст каждого запроса
BRIEF_FIELDS = ("niche", "audience", "goal", "channel", "constraints")


class Brief(Base):
    __tablename__ = 'briefs'
    user_id = Column(BigInteger, primary_key=True)
    niche = Column(Text, nullable=True)
    audience = Column(Text, nullable=True)
    goal = Column(Text, nullable=True)
    channel = Column(Text, nullable=True)
    constraints = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ProjectFolder(Base):
    """
    Изолированная папка проекта (v2.1): хранит собственные 5 полей брифа,
    пин регистра (HARD_B2B | CREATOR_MEDIA | PR_BRAND | None),
    накопленную редполитику (editorial_rules_json) и историю чата (history_json).
    """
    __tablename__ = 'projects'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, index=True, nullable=False)
    slot_num = Column(Integer, default=1)
    title = Column(String, default="Проект №1")
    pinned_register = Column(String, nullable=True)  # None = Авто-определение, иначе HARD_B2B / CREATOR_MEDIA / PR_BRAND
    niche = Column(Text, nullable=True)
    audience = Column(Text, nullable=True)
    goal = Column(Text, nullable=True)
    channel = Column(Text, nullable=True)
    constraints = Column(Text, nullable=True)
    editorial_rules_json = Column(Text, default="[]")
    history_json = Column(Text, default="[]")
    arc_ledger_json = Column(Text, default="[]")
    voice_samples_json = Column(Text, default="[]")
    updated_at = Column(DateTime, default=datetime.utcnow)

    @property
    def slot_index(self) -> int:
        return self.slot_num or 1


class DatabaseService:
    def __init__(self, db_url):
        self.engine = create_async_engine(db_url, echo=False)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for col_sql in (
                "ALTER TABLE users ADD COLUMN max_projects INTEGER DEFAULT 2",
                "ALTER TABLE users ADD COLUMN active_project_id INTEGER",
                "ALTER TABLE projects ADD COLUMN arc_ledger_json TEXT DEFAULT '[]'",
                "ALTER TABLE projects ADD COLUMN voice_samples_json TEXT DEFAULT '[]'",
            ):
                try:
                    await conn.execute(sql_text(col_sql))
                except Exception:
                    pass

    async def log_event(self, user_id: int, event_type: str, event_data: str = None):
        """Логирование любого события пользователя для сквозной аналитики"""
        async with self.async_session() as session:
            event = AnalyticsEvent(user_id=user_id, event_type=event_type, event_data=event_data)
            session.add(event)
            await session.commit()

    async def get_user(self, user_id: int):
        async with self.async_session() as session:
            return await session.get(User, user_id)

    async def get_or_create_active_project(self, user_id: int) -> ProjectFolder:
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user and user.active_project_id:
                proj = await session.get(ProjectFolder, user.active_project_id)
                if proj and proj.user_id == user_id:
                    return proj
            stmt = select(ProjectFolder).where(ProjectFolder.user_id == user_id).order_by(ProjectFolder.id.asc())
            res = await session.execute(stmt)
            proj = res.scalars().first()
            if not proj:
                # Если в старой таблице briefs уже был бриф — переносим в Папку №1
                old_brief = await session.get(Brief, user_id)
                proj = ProjectFolder(
                    user_id=user_id,
                    slot_num=1,
                    title="Проект №1",
                    niche=old_brief.niche if old_brief else None,
                    audience=old_brief.audience if old_brief else None,
                    goal=old_brief.goal if old_brief else None,
                    channel=old_brief.channel if old_brief else None,
                    constraints=old_brief.constraints if old_brief else None,
                )
                session.add(proj)
                await session.flush()
            if user:
                user.active_project_id = proj.id
            await session.commit()
            return proj

    async def list_projects(self, user_id: int) -> list[ProjectFolder]:
        await self.get_or_create_active_project(user_id)
        async with self.async_session() as session:
            stmt = select(ProjectFolder).where(ProjectFolder.user_id == user_id).order_by(ProjectFolder.slot_num.asc(), ProjectFolder.id.asc())
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def switch_active_project(self, user_id: int, project_id: int) -> ProjectFolder | None:
        async with self.async_session() as session:
            proj = await session.get(ProjectFolder, project_id)
            if not proj or proj.user_id != user_id:
                return None
            user = await session.get(User, user_id)
            if user:
                user.active_project_id = proj.id
                await session.commit()
            return proj

    async def create_new_project(self, user_id: int, title: str | None = None) -> tuple[ProjectFolder | None, int]:
        """
        Создаёт новую изолированную папку проекта и делает её активной.
        Если достигнут лимит тарифа (2 на демо/1490, 3 на Эксперте 4900, 10 на Продюсере 9900),
        возвращает (None, max_allowed).
        """
        is_unlim = await self.has_unlimited(user_id)
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            max_allowed = 10 if is_unlim else (user.max_projects if user and user.max_projects else 2)
            stmt = select(ProjectFolder).where(ProjectFolder.user_id == user_id).order_by(ProjectFolder.id.asc())
            existing = list((await session.execute(stmt)).scalars().all())
            if len(existing) >= max_allowed:
                return None, max_allowed
            next_slot = len(existing) + 1
            proj = ProjectFolder(
                user_id=user_id,
                slot_num=next_slot,
                title=(title or f"Проект №{next_slot}")[:48],
            )
            session.add(proj)
            await session.flush()
            if user:
                user.active_project_id = proj.id
            await session.commit()
            return proj, max_allowed

    async def set_project_pinned_register(self, user_id: int, pinned_register: str | None) -> ProjectFolder:
        proj = await self.get_or_create_active_project(user_id)
        async with self.async_session() as session:
            db_proj = await session.get(ProjectFolder, proj.id)
            db_proj.pinned_register = pinned_register
            db_proj.updated_at = datetime.utcnow()
            await session.commit()
            return db_proj

    async def get_editorial_rules(self, user_id: int) -> list[str]:
        proj = await self.get_or_create_active_project(user_id)
        try:
            rules = json.loads(proj.editorial_rules_json or "[]")
            return [str(r).strip() for r in rules if str(r).strip()] if isinstance(rules, list) else []
        except Exception:
            return []

    async def save_editorial_rules(self, user_id: int, rules: list[str]):
        proj = await self.get_or_create_active_project(user_id)
        clean_rules = []
        for r in rules:
            s = str(r).strip()
            if s and s not in clean_rules:
                clean_rules.append(s[:600])
        clean_rules = clean_rules[-12:]
        async with self.async_session() as session:
            db_proj = await session.get(ProjectFolder, proj.id)
            db_proj.editorial_rules_json = json.dumps(clean_rules, ensure_ascii=False)
            db_proj.updated_at = datetime.utcnow()
            await session.commit()

    async def get_project_history(self, user_id: int) -> list[dict]:
        proj = await self.get_or_create_active_project(user_id)
        try:
            h = json.loads(proj.history_json or "[]")
            return h if isinstance(h, list) else []
        except Exception:
            return []

    async def save_project_history(self, user_id: int, history: list[dict]):
        proj = await self.get_or_create_active_project(user_id)
        async with self.async_session() as session:
            db_proj = await session.get(ProjectFolder, proj.id)
            db_proj.history_json = json.dumps((history or [])[-24:], ensure_ascii=False)
            db_proj.updated_at = datetime.utcnow()
            await session.commit()

    async def get_arc_ledger(self, user_id: int) -> list[dict]:
        proj = await self.get_or_create_active_project(user_id)
        try:
            items = json.loads(proj.arc_ledger_json or "[]")
            return items if isinstance(items, list) else []
        except Exception:
            return []

    async def append_arc_ledger_entry(self, user_id: int, entry: dict):
        proj = await self.get_or_create_active_project(user_id)
        async with self.async_session() as session:
            db_proj = await session.get(ProjectFolder, proj.id)
            try:
                items = json.loads(db_proj.arc_ledger_json or "[]")
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
            next_n = (items[-1].get("n", len(items)) + 1) if items else 1
            entry_clean = {
                "n": entry.get("n") or next_n,
                "genre": str(entry.get("genre", "POST"))[:32],
                "lead_hook": str(entry.get("lead_hook", "")).strip()[:120],
                "facts": str(entry.get("facts", "")).strip()[:150],
                "metaphors": list(entry.get("metaphors") or [])[:5],
            }
            items.append(entry_clean)
            db_proj.arc_ledger_json = json.dumps(items[-30:], ensure_ascii=False)
            db_proj.updated_at = datetime.utcnow()
            await session.commit()

    async def get_voice_samples(self, user_id: int) -> list[str]:
        proj = await self.get_or_create_active_project(user_id)
        try:
            samples = json.loads(proj.voice_samples_json or "[]")
            return [str(s).strip() for s in samples if str(s).strip()] if isinstance(samples, list) else []
        except Exception:
            return []

    async def save_voice_samples(self, user_id: int, samples: list[str]):
        proj = await self.get_or_create_active_project(user_id)
        clean = [str(s).strip()[:600] for s in (samples or []) if str(s).strip()][:2]
        async with self.async_session() as session:
            db_proj = await session.get(ProjectFolder, proj.id)
            db_proj.voice_samples_json = json.dumps(clean, ensure_ascii=False)
            db_proj.updated_at = datetime.utcnow()
            await session.commit()

    async def get_all_user_model_texts(self, user_id: int) -> str:
        """Собирает все прошлые ответы модели и arc_ledger по ВСЕМ папкам user_id (для сквозного анти-повтора референсов)."""
        async with self.async_session() as session:
            stmt = select(ProjectFolder).where(ProjectFolder.user_id == user_id)
            projects = list((await session.execute(stmt)).scalars().all())
        chunks = []
        for p in projects:
            try:
                hist = json.loads(p.history_json or "[]")
                for m in hist:
                    if m.get("role") == "model":
                        txt = " ".join(pt.get("text", "") for pt in m.get("parts", []) if pt.get("text"))
                        if txt:
                            chunks.append(txt)
            except Exception:
                pass
            try:
                ledger = json.loads(p.arc_ledger_json or "[]")
                for item in ledger:
                    chunks.append(" ".join(item.get("metaphors") or []))
                    if item.get("lead_hook"):
                        chunks.append(item["lead_hook"])
            except Exception:
                pass
        return "\n".join(chunks)

    async def get_brief(self, user_id: int) -> dict | None:
        proj = await self.get_or_create_active_project(user_id)
        data = {f: getattr(proj, f) for f in BRIEF_FIELDS if getattr(proj, f)}
        return data if data else None

    async def save_brief(self, user_id: int, answers: dict):
        proj = await self.get_or_create_active_project(user_id)
        async with self.async_session() as session:
            db_proj = await session.get(ProjectFolder, proj.id)
            for f in BRIEF_FIELDS:
                max_len = 5500 if f == "channel" else 2000
                val = (answers.get(f) or "").strip()[:max_len] or None
                setattr(db_proj, f, val)
            niche_val = (answers.get("niche") or "").strip()
            if niche_val and (not db_proj.title or db_proj.title.startswith(("Проект №", "📁 "))):
                cleaned_niche = re.sub(
                    r"(?i)^(?:я\s+(?:продюсер|эксперт|основатель|владелец|директор|партн[её]р|руководитель|веду)[^.\n—-]{0,40}[.\n—-]+\s*)?"
                    r"(?:(?:первый|второй|текущий|мой|наш)\s+проект\s*[—:-]\s*)?",
                    "",
                    niche_val,
                ).strip() or niche_val
                raw_short = re.split(r"[.,;(\n—-]", cleaned_niche)[0].strip()
                raw_short = re.sub(r"^[^\wА-Яа-яЁё]+", "", raw_short).strip()
                words = raw_short.split()
                clean_title = ""
                for w in words:
                    cand = f"{clean_title} {w}".strip() if clean_title else w
                    if len(cand) <= 18:
                        clean_title = cand
                    else:
                        break
                if not clean_title and raw_short:
                    clean_title = raw_short[:16].rstrip()
                if clean_title:
                    db_proj.title = clean_title
            db_proj.updated_at = datetime.utcnow()
            # Синхронно обновляем и старую таблицу briefs для обратной совместимости
            brief = await session.get(Brief, user_id) or Brief(user_id=user_id)
            for f in BRIEF_FIELDS:
                setattr(brief, f, getattr(db_proj, f))
            brief.updated_at = datetime.utcnow()
            session.add(brief)
            await session.commit()

    async def create_user(self, user_id: int, username: str, full_name: str):
        async with self.async_session() as session:
            new_user = User(id=user_id, username=username, full_name=full_name)
            session.add(new_user)
            await session.commit()
            return new_user

    async def update_balance(self, user_id: int, delta: int, add_days: int = 0):
        """Изменяет баланс кредитов и устанавливает время жизни подписки."""
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if not user:
                return 0

            # Логика обновления даты: если добавляем дни, пересчитываем от текущего момента
            new_expires_at = user.expires_at
            if add_days > 0:
                new_expires_at = datetime.utcnow() + timedelta(days=add_days)
            elif delta < 0 and user.expires_at and datetime.utcnow() > user.expires_at:
                # Если подписка истекла, обнуляем баланс (при попытке списать)
                user.credits = 0
                user.expires_at = None
                await session.commit()
                return 0

            new_credits = user.credits + delta
            if new_credits < 0:
                new_credits = 0

            # Авто-повышение лимита папок проектов при покупке тарифов Эксперт (60 разб. -> 3 папки) и Продюсер (200 разб. -> 10 папок)
            new_max_projects = user.max_projects or 2
            if delta >= 150:
                new_max_projects = max(new_max_projects, 10)
            elif delta >= 50:
                new_max_projects = max(new_max_projects, 3)

            stmt = (
                update(User)
                .where(User.id == user_id)
                .values(credits=new_credits, expires_at=new_expires_at, max_projects=new_max_projects)
                .returning(User.credits)
            )
            result = await session.execute(stmt)
            new_balance = result.scalar_one_or_none()
            await session.commit()
            
            if new_balance is not None:
                return new_balance
            return 0

    async def reset_balance_if_expired(self, user_id: int) -> bool:
        """Проверяет подписку, если истекла - сбрасывает баланс. Возвращает True если сбросил."""
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user and user.expires_at and datetime.utcnow() > user.expires_at:
                user.credits = 0
                user.expires_at = None
                await session.commit()
                return True
        return False

    async def update_temp(self, user_id: int, temp: float):
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user:
                user.temperature = temp
                await session.commit()

    async def add_stars_transaction(self, user_id: int, stars: int, credits: int, charge_id: str):
        async with self.async_session() as session:
            tx = Transaction(
                user_id=user_id, 
                amount=stars, 
                credits_added=credits, 
                description="Stars Topup", 
                telegram_charge_id=charge_id
            )
            session.add(tx)
            await session.commit()
            
    async def check_tx_exists(self, charge_id: str):
        from sqlalchemy import select
        async with self.async_session() as session:
             stmt = select(Transaction).where(Transaction.telegram_charge_id == charge_id)
             result = await session.execute(stmt)
             return result.scalar_one_or_none() is not None

    # [FIX] Новые методы для управления промокодами
    async def activate_promo(self, user_id: int, hours: int = 24) -> bool:
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user:
                user.unlimited_until = datetime.utcnow() + timedelta(hours=hours)
                await session.commit()
                return True
        return False

    # [FIX] Обновленный метод проверки безлимита с учетом JSON
    async def has_unlimited(self, user_id: int) -> bool:
        # 1. Сначала проверяем жестко заданные VIP доступы из JSON
        # [КРИТИЧЕСКИЙ ФИКС] Абсолютный путь — больше не зависит от cwd
        base_dir = os.path.dirname(os.path.abspath(__file__))
        promo_file = os.path.join(base_dir, 'promo_users.json')
        if os.path.exists(promo_file):
            try:
                with open(promo_file, 'r', encoding='utf-8') as f:
                    promo_data = json.load(f)
                
                str_id = str(user_id)
                if str_id in promo_data:
                    exp_date_str = promo_data[str_id]
                    # Формат в JSON: дд-мм-гггг
                    exp_date = datetime.strptime(exp_date_str, "%d-%m-%Y")
                    # Если текущая дата меньше или равна дате истечения - даем безлимит
                    if datetime.utcnow() <= exp_date:
                        return True
            except Exception as e:
                logger.error(f"⚠️ Ошибка чтения promo_users.json: {e}")

        # 2. Если в JSON юзера нет или доступ истек, идем в стандартную БД
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user and user.unlimited_until:
                if datetime.utcnow() <= user.unlimited_until:
                    return True
                else:
                    # Если время безлимита вышло, аккуратно обнуляем поле
                    user.unlimited_until = None
                    await session.commit()
        return False

    # === [МЕТОДЫ УПРАВЛЕНИЯ ВНУТРЕННИМИ СОСТОЯНИЯМИ] ===
    async def update_user_state(self, user_id: int, trust_delta: int = 0, fatigue_delta: int = 0, new_mood: str = None) -> dict:
        """Динамически обновляет внутренние состояния Аристарха и возвращает их."""
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user:
                user.trust_level = max(0, min(100, user.trust_level + trust_delta))
                user.fatigue = max(0, min(100, user.fatigue + fatigue_delta))
                
                if new_mood:
                    user.mood = new_mood
                    
                await session.commit()
                return {"trust": user.trust_level, "fatigue": user.fatigue, "mood": user.mood}
        return {"trust": 50, "fatigue": 0, "mood": "neutral"}

    async def reset_fatigue(self, user_id: int):
        """Сбрасывает усталость Аристарха после отдыха."""
        async with self.async_session() as session:
            user = await session.get(User, user_id)
            if user:
                user.fatigue = 0
                user.mood = "neutral"
                await session.commit()
                return True
        return False

    # === [NEW] МЕТОДЫ ДЛЯ НОЧНОЙ РЕФЛЕКСИИ (СОН АРИСТАРХА) ===
    
    async def get_active_users_last_24h(self) -> list[int]:
        """
        Возвращает список ID пользователей, которые отправляли сообщения боту 
        за последние 24 часа. Используется worker_cron.py для определения 
        активных юзеров, нуждающихся в ночной рефлексии.
        """
        from sqlalchemy import select, distinct
        async with self.async_session() as session:
            yesterday = datetime.utcnow() - timedelta(days=1)
            stmt = select(distinct(AnalyticsEvent.user_id)).where(
                AnalyticsEvent.timestamp >= yesterday,
                AnalyticsEvent.event_type == "USER_MESSAGE"  # Только реальные сообщения, не системные
            )
            result = await session.execute(stmt)
            user_ids = result.scalars().all()
            logger.info(f"📊 [Ночная рефлексия] Найдено активных юзеров: {len(user_ids)}")
            return user_ids

    async def get_user_dialogue_last_24h(self, user_id: int) -> str:
        """
        Собирает полный диалог пользователя и Аристарха за последние 24 часа 
        в единый склеенный транскрипт для анализа Flash-моделью.
        
        Возвращает текст в формате:
        [КЛИЕНТ]: сообщение пользователя
        [АРИСТАРХ]: ответ бота
        ...
        """
        from sqlalchemy import select
        async with self.async_session() as session:
            yesterday = datetime.utcnow() - timedelta(days=1)
            stmt = select(AnalyticsEvent).where(
                AnalyticsEvent.user_id == user_id,
                AnalyticsEvent.event_type.in_(["USER_MESSAGE", "AI_RESPONSE_TEXT"]),
                AnalyticsEvent.timestamp >= yesterday
            ).order_by(AnalyticsEvent.timestamp.asc())
            
            result = await session.execute(stmt)
            events = result.scalars().all()

            dialogue = []
            for ev in events:
                role = "КЛИЕНТ" if ev.event_type == "USER_MESSAGE" else "АРИСТАРХ"
                if ev.event_data:
                    # Очищаем текст от возможных артефактов
                    clean_text = ev.event_data.strip()
                    dialogue.append(f"[{role}]: {clean_text}")
            
            transcript = "\n\n".join(dialogue)
            logger.info(f"📝 [Ночная рефлексия] Собран диалог для юзера {user_id}: {len(transcript)} символов")
            return transcript