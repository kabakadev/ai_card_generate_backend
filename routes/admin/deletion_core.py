# routes/admin/deletion_core.py
"""
Core deletion logic with performance strategies.
"""
from __future__ import annotations

import time
from sqlalchemy import delete, select
from config import app, db
from models import User, Deck, Progress, Flashcard


def bulk_delete_users_by_ids(user_ids: list[int]) -> dict:
    """
    FAST: Explicit set-based deletes for all child tables, then users.
    
    Performance: ~1000 users/sec
    One transaction; returns row counts.
    """
    # 1) Delete progress owned by these users
    res1 = db.session.execute(
        delete(Progress).where(Progress.user_id.in_(user_ids))
    )
    progress_deleted = res1.rowcount or 0

    # 2) Delete flashcards via their decks
    deck_ids_sel = select(Deck.id).where(Deck.user_id.in_(user_ids))
    res2 = db.session.execute(
        delete(Flashcard).where(Flashcard.deck_id.in_(deck_ids_sel))
    )
    flashcards_deleted = res2.rowcount or 0

    # 3) Delete decks
    res3 = db.session.execute(
        delete(Deck).where(Deck.user_id.in_(user_ids))
    )
    decks_deleted = res3.rowcount or 0

    # 4) Finally delete users (DB will cascade anything else with FK ondelete=CASCADE)
    res4 = db.session.execute(
        delete(User).where(User.id.in_(user_ids))
    )
    users_deleted = res4.rowcount or 0

    return {
        "progress": progress_deleted,
        "flashcards": flashcards_deleted,
        "decks": decks_deleted,
        "users": users_deleted,
    }


def delete_users_slow(user_ids: list[int], commit_per: str = "user", sleep_ms: int = 0) -> dict:
    """
    INTENTIONALLY SLOW: ORM loops + frequent commits + optional sleeps.
    
    Performance: ~10 users/sec
    For debugging and testing slow query scenarios.
    
    Args:
        commit_per: "user" (commit after each user) or "row" (after each row)
        sleep_ms: Milliseconds to sleep between operations
    """
    t0 = time.perf_counter()
    total = {"progress": 0, "flashcards": 0, "decks": 0, "users": 0}

    # Load users with default lazy loading (creates N+1 queries intentionally)
    users = (
        db.session.query(User)
        .filter(User.id.in_(user_ids))
        .all()
    )

    for u in users:
        # 1) Delete progress one-by-one
        for p in db.session.query(Progress).filter_by(user_id=u.id).all():
            db.session.delete(p)
            total["progress"] += 1
            if commit_per == "row":
                db.session.commit()
                if sleep_ms:
                    time.sleep(sleep_ms / 1000.0)

        # 2) Delete flashcards by traversing decks
        for d in db.session.query(Deck).filter_by(user_id=u.id).all():
            for c in db.session.query(Flashcard).filter_by(deck_id=d.id).all():
                db.session.delete(c)
                total["flashcards"] += 1
                if commit_per == "row":
                    db.session.commit()
                    if sleep_ms:
                        time.sleep(sleep_ms / 1000.0)
            
            db.session.delete(d)
            total["decks"] += 1
            if commit_per == "row":
                db.session.commit()
                if sleep_ms:
                    time.sleep(sleep_ms / 1000.0)

        # 3) Delete the user
        db.session.delete(u)
        total["users"] += 1

        if commit_per == "user":
            db.session.commit()
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)
        elif commit_per == "row":
            db.session.commit()
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)

    # One big commit at end if not already committed
    if commit_per not in {"user", "row"}:
        db.session.commit()

    total["elapsed_s"] = round(time.perf_counter() - t0, 3)
    total["mode"] = f"slow(commit_per={commit_per}, sleep_ms={sleep_ms})"
    return total


def delete_users_turbo(user_ids: list[int]) -> dict:
    """
    TURBO: Delete only users and let FK ON DELETE CASCADE clear children.
    
    Performance: ~2000 users/sec
    Fastest but requires proper FK constraints in database.
    """
    t0 = time.perf_counter()
    res = db.session.execute(delete(User).where(User.id.in_(user_ids)))
    db.session.commit()
    
    return {
        "progress": None,
        "flashcards": None,
        "decks": None,
        "users": res.rowcount or 0,
        "elapsed_s": round(time.perf_counter() - t0, 3),
        "mode": "turbo(fk_cascade)"
    }


def delete_users_fast(user_ids: list[int]) -> dict:
    """
    FAST: Set-based deletes with timing.
    
    Performance: ~1000 users/sec
    Recommended for production use.
    """
    t0 = time.perf_counter()
    counts = bulk_delete_users_by_ids(user_ids)
    db.session.commit()
    
    counts["elapsed_s"] = round(time.perf_counter() - t0, 3)
    counts["mode"] = "fast(set_based)"
    return counts


def execute_deletion_strategy(
    user_ids: list[int],
    strategy: str = "fast",
    commit_per: str = "user",
    sleep_ms: int = 0,
    echo_sql: bool = False
) -> dict:
    """
    Execute user deletion with specified strategy.
    
    Args:
        user_ids: List of user IDs to delete
        strategy: "fast" (default), "slow", or "turbo"
        commit_per: Only used for "slow" - "user" or "row"
        sleep_ms: Only used for "slow" - milliseconds between operations
        echo_sql: Enable SQL query logging for debugging
    
    Returns:
        Dict with deletion counts and performance metrics
    """
    from .utils import temp_echo_sql
    
    strategy = strategy.lower().strip()
    
    with temp_echo_sql(db.engine, echo_sql):
        if strategy == "slow":
            return delete_users_slow(user_ids, commit_per=commit_per, sleep_ms=sleep_ms)
        elif strategy == "turbo":
            return delete_users_turbo(user_ids)
        else:
            return delete_users_fast(user_ids)