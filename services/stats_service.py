from datetime import datetime, timedelta
from sqlalchemy import func, case
from config import db
from models import ReviewLog


class StatsService:
    """Service for calculating user statistics from review logs."""
    
    @staticmethod
    def get_accuracy(user_id: int, days: int = None) -> float:
        """
        Calculate accuracy for a user.
        
        Args:
            user_id: User ID
            days: If provided, only calculate for last N days. If None, all-time.
        
        Returns:
            Float between 0.0 and 1.0 (e.g., 0.85 = 85%)
        
        Example:
            accuracy = StatsService.get_accuracy(user_id=1)  # all-time
            accuracy = StatsService.get_accuracy(user_id=1, days=7)  # last 7 days
        """
        query = db.session.query(
            func.count(ReviewLog.id).label("total"),
            func.sum(case((ReviewLog.was_correct == True, 1), else_=0)).label("correct"),
        ).filter(ReviewLog.user_id == user_id)

        if days and days > 0:
            cutoff = datetime.utcnow() - timedelta(days=days)
            query = query.filter(ReviewLog.created_at >= cutoff)

        result = query.first()

        if not result or not result.total:
            print(f"[StatsService] get_accuracy({user_id}, {days}): no reviews found")
            return 0.0

        correct = result.correct or 0
        accuracy = float(correct) / float(result.total)
        print(f"[StatsService] get_accuracy({user_id}, {days}): {correct}/{result.total} = {accuracy:.4f}")
        return accuracy
    
    @staticmethod
    def get_daily_accuracy(user_id: int, days: int = 7) -> list:
        """
        Get accuracy broken down by day for the last N days.
        
        Args:
            user_id: User ID
            days: Number of days to look back (default 7)
        
        Returns:
            List of dicts: 
            [
                {"date": "2025-10-11", "accuracy": 0.82, "total": 10, "correct": 8},
                ...
            ]
        
        Example:
            data = StatsService.get_daily_accuracy(user_id=1, days=7)
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        results = db.session.query(
            func.date(ReviewLog.created_at).label('date'),
            func.count(ReviewLog.id).label('total'),
            func.sum(func.cast(ReviewLog.was_correct, db.Integer)).label('correct')
        ).filter(
            ReviewLog.user_id == user_id,
            ReviewLog.created_at >= cutoff
        ).group_by(func.date(ReviewLog.created_at)).order_by(func.date(ReviewLog.created_at)).all()
        
        return [
            {
                'date': r.date.isoformat() if r.date else None,
                'accuracy': float(r.correct or 0) / float(r.total) if r.total > 0 else 0.0,
                'total': r.total,
                'reviews': r.total,
                'correct': r.correct or 0
            }
            for r in results
        ]
    
    @staticmethod
    def get_time_studied(user_id: int, days: int = 7) -> dict:
        """
        Get minutes studied per day for the last N days.
        
        Args:
            user_id: User ID
            days: Number of days to look back (default 7)
        
        Returns:
            {
                "daily": [
                    {"date": "2025-10-11", "minutes": 34.5},
                    ...
                ],
                "total_minutes": 123.5
            }
        
        Example:
            data = StatsService.get_time_studied(user_id=1, days=7)
            print(f"Total this week: {data['total_minutes']} minutes")
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        seconds_query = db.session.query(
            func.sum(ReviewLog.time_spent_seconds).label("total_seconds")
        ).filter(
            ReviewLog.user_id == user_id,
            ReviewLog.created_at >= cutoff
        )
        total_seconds = seconds_query.scalar() or 0

        daily_rows = db.session.query(
            func.date(ReviewLog.created_at).label("date"),
            func.sum(ReviewLog.time_spent_seconds).label("seconds"),
        ).filter(
            ReviewLog.user_id == user_id,
            ReviewLog.created_at >= cutoff,
        ).group_by(func.date(ReviewLog.created_at)).order_by(func.date(ReviewLog.created_at)).all()

        daily_breakdown = [
            {
                'date': row.date.isoformat() if row.date else None,
                'minutes': round((row.seconds or 0) / 60.0, 2),
                'seconds': int(row.seconds or 0),
            }
            for row in daily_rows
        ]

        total_minutes = round(total_seconds / 60.0, 2)
        daily_minutes = round(total_minutes / float(days), 2) if days else round(total_minutes, 2)

        print(f"[StatsService] get_time_studied({user_id}, {days}): {total_seconds}s = {total_minutes}min")

        return {
            'total_seconds': int(total_seconds),
            'total_minutes': total_minutes,
            'daily_minutes': daily_minutes,
            'daily': daily_breakdown,
        }
    
    @staticmethod
    def get_total_reviews(user_id: int, days: int = None) -> int:
        """
        Get total number of review attempts.
        
        Args:
            user_id: User ID
            days: If provided, only count last N days. If None, all-time.
        
        Returns:
            Integer count of reviews
        
        Example:
            count = StatsService.get_total_reviews(user_id=1)  # all-time
            count = StatsService.get_total_reviews(user_id=1, days=7)  # this week
        """
        query = ReviewLog.query.filter_by(user_id=user_id)
        if days and days > 0:
            cutoff = datetime.utcnow() - timedelta(days=days)
            query = query.filter(ReviewLog.created_at >= cutoff)
        return query.count()
    
    @staticmethod
    def get_weak_cards(user_id: int, deck_id: int = None, limit: int = 5) -> list:
        """
        Get flashcards with lowest accuracy for a user.
        
        Args:
            user_id: User ID
            deck_id: If provided, only cards from this deck. If None, all decks.
            limit: Number of cards to return (default 5)
        
        Returns:
            List of dicts:
            [
                {"flashcard_id": 1, "accuracy": 0.33, "total_attempts": 3, "correct": 1},
                ...
            ]
        
        Example:
            weak = StatsService.get_weak_cards(user_id=1, deck_id=5, limit=10)
        """
        query = db.session.query(
            ReviewLog.flashcard_id,
            func.count(ReviewLog.id).label('total'),
            func.sum(func.cast(ReviewLog.was_correct, db.Integer)).label('correct')
        ).filter(ReviewLog.user_id == user_id)
        
        if deck_id:
            query = query.filter(ReviewLog.deck_id == deck_id)
        
        results = query.group_by(ReviewLog.flashcard_id).order_by(
            (func.sum(func.cast(ReviewLog.was_correct, db.Integer)).cast(db.Float) / 
             func.count(ReviewLog.id).cast(db.Float)).asc()
        ).limit(limit).all()
        
        return [
            {
                'flashcard_id': r.flashcard_id,
                'accuracy': float(r.correct) / float(r.total) if r.total > 0 else 0.0,
                'total_attempts': r.total,
                'correct': r.correct
            }
            for r in results
        ]
