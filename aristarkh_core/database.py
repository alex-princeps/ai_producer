import json
import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger, Text, update
from aristarkh_core.config import logger # Добавили логгер для отслеживания ошибок JSON

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    credits = Column(Integer, default=10) # [FIX] 10 бесплатных первых запросов
    expires_at = Column(DateTime, nullable=True) # [FIX] Дата сгорания подписки
    temperature = Column(Float, default=0.7)
    created_at = Column(DateTime, default=datetime.utcnow)
    # [FIX] Поле для хранения срока действия безлимита
    unlimited_until = Column(DateTime, nullable=True)
    
    # === [STATE TRACKER: АФФЕКТИВНАЯ ПАМЯТЬ] ===
    trust_level = Column(Integer, default=50) # От 0 (ненависть) до 100 (бро)
    fatigue = Column(Integer, default=0) # От 0 до 100, растет в сессии
    mood = Column(String, default="neutral") # Текущее настроение: neutral, sarcastic, angry, bored
    
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


class DatabaseService:
    def __init__(self, db_url):
        self.engine = create_async_engine(db_url, echo=False)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def log_event(self, user_id: int, event_type: str, event_data: str = None):
        """Логирование любого события пользователя для сквозной аналитики"""
        async with self.async_session() as session:
            event = AnalyticsEvent(user_id=user_id, event_type=event_type, event_data=event_data)
            session.add(event)
            await session.commit()

    async def get_user(self, user_id: int):
        async with self.async_session() as session:
            return await session.get(User, user_id)

    async def get_brief(self, user_id: int) -> dict | None:
        async with self.async_session() as session:
            brief = await session.get(Brief, user_id)
            return {f: getattr(brief, f) for f in BRIEF_FIELDS} if brief else None

    async def save_brief(self, user_id: int, answers: dict):
        async with self.async_session() as session:
            brief = await session.get(Brief, user_id) or Brief(user_id=user_id)
            for f in BRIEF_FIELDS:
                setattr(brief, f, (answers.get(f) or "").strip()[:2000] or None)
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

            stmt = (
                update(User)
                .where(User.id == user_id)
                .values(credits=new_credits, expires_at=new_expires_at)
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