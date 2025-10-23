# FlashLearn Backend API Contract (Progress & Dashboard)

This document captures the REST contract that the frontend should rely on after the review logging refactor.

## Authentication

All endpoints require a valid JWT access token supplied via the `Authorization: Bearer <token>` header.

---

## POST `/progress`

Creates or updates the student's `Progress` row for a flashcard **and** appends a `ReviewLog` entry for analytical tracking. The operation is atomic—if any portion fails, no rows are persisted.

### Request Body

```json
{
  "flashcard_id": 123,
  "deck_id": 45,
  "was_correct": true,
  "time_spent_seconds": 8
}
```

Field notes:

| Field               | Type    | Required | Description                                             |
|---------------------|---------|----------|---------------------------------------------------------|
| `flashcard_id`      | int     | ✓        | Flashcard being reviewed                                |
| `deck_id`           | int     | ✓        | Deck containing the flashcard                           |
| `was_correct`       | boolean | ✓        | Whether the answer was correct                          |
| `time_spent_seconds`| int     | ✕ (default 0) | Time spent on the attempt, rounded to whole seconds |
| `track_reviews`     | boolean | ✕ (default `true`) | When `false`, the review is acknowledged but not logged |

### Response (200 OK)

```json
{
  "id": 91,
  "user_id": 1,
  "flashcard_id": 123,
  "deck_id": 45,
  "study_count": 7,
  "correct_attempts": 4,
  "incorrect_attempts": 3,
  "total_study_time": 52.5,
  "review_status": "learning",
  "is_learned": false,
  "was_correct": true,
  "time_spent_seconds": 8,
  "message": "Progress and review logged successfully",
  "review_log": {
    "id": 789,
    "user_id": 1,
    "flashcard_id": 123,
    "deck_id": 45,
    "was_correct": true,
    "time_spent_seconds": 8,
    "created_at": "2025-10-16T10:30:00Z"
  },
  "stats": {
    "accuracy": 0.82,
    "accuracy_pct": 82.0,
    "retention_rate": 82.0,
    "focus_score": 63.5,
    "total_minutes_last_7_days": 215.4,
    "total_seconds_last_7_days": 12924,
    "total_reviews": 142,
    "weak_cards_count": 3
  },
  "tracked": true
}
```

Key guarantees:

- `total_study_time` is stored in minutes (float).
- `review_log.created_at` is UTC ISO-8601.
- `stats.accuracy` is a ratio (0.0–1.0); `accuracy_pct` mirrors UserStats (0–100).
- `total_seconds_last_7_days` exposes raw seconds used for average time calculations.
- `tracked` indicates whether the submission updated progress/analytics (respects the optional `track_reviews` flag).

### Error Responses

| Status | Body                                               | When                                               |
|--------|----------------------------------------------------|---------------------------------------------------|
| 400    | `{"error": "Missing fields: ..."} `                | Required field missing                            |
| 401    | `{"error": "invalid token payload"}`               | JWT absent/invalid                                |
| 500    | `{"error": "Failed to record progress. Please try again."}` | Unexpected persistence issue                |

---

## GET `/dashboard`

Returns deck-level rollups and a `stats` block powered by `StatsService`.

### Response (200 OK)

```json
{
  "username": "flash_student",
  "total_flashcards_studied": 142,
  "most_reviewed_deck": "US History",
  "weekly_goal": 20,
  "mastery_level": 82.0,
  "study_streak": 4,
  "focus_score": 63.5,
  "retention_rate": 82.0,
  "cards_mastered": 18,
  "minutes_per_day": 32.5,
  "accuracy": 82.0,
  "decks": [
    {
      "deck_id": 45,
      "deck_title": "US History",
      "flashcards_studied": 62
    },
    {
      "deck_id": 46,
      "deck_title": "Chemistry Fundamentals",
      "flashcards_studied": 80
    }
  ],
  "stats": {
    "accuracy": 0.82,
    "accuracy_pct": 82.0,
    "total_reviews": 142,
    "time_studied_today": 45.0,
    "time_studied_week": 230.0,
    "time_studied_week_daily": [
      {"date": "2025-10-16", "minutes": 45.0},
      {"date": "2025-10-15", "minutes": 32.0}
    ],
    "daily_accuracy": [
      {"date": "2025-10-16", "accuracy": 0.88, "reviews": 12, "correct": 11},
      {"date": "2025-10-15", "accuracy": 0.82, "reviews": 18, "correct": 15}
    ],
    "weak_cards": [
      {
        "flashcard_id": 902,
        "question": "Explain Newton's 2nd Law",
        "deck_id": 46,
        "deck_name": "Physics Essentials",
        "accuracy": 0.33,
        "attempts": 6,
        "correct": 2
      }
    ],
    "focus_score": 63.5
  }
}
```

Notes:
- `stats.accuracy` is the all-time ratio (0.0–1.0).
- `time_studied_week_daily` aligns with `StatsService.get_time_studied(..., days=7)["daily"]`.
- `daily_accuracy` derives from `StatsService.get_daily_accuracy(..., days=7)` with `reviews` reflecting total attempts.
- `weak_cards` enriches `StatsService.get_weak_cards(...)` with flashcard front text and deck metadata.

## Dashboard Stats Definitions

All metrics under `stats` are calculated directly from the `review_logs` table via `StatsService`.

- **accuracy** *(float)* – Proportion of correct answers. Formula: `correct_reviews / total_reviews`. Example: `0.864` → 86.4%.
- **total_reviews** *(int)* – Total number of flashcard attempts (all time). Increments by one every time the student submits an answer.
- **time_studied_minutes** *(float)* – Minutes spent during the past 7 days. Derived from `time_spent_seconds` values in `ReviewLog`.
- **avg_time_per_card** *(float seconds)* – Average seconds spent per review in the past 7 days. Formula: `total_seconds_last_7_days / total_reviews`.
- **daily_accuracy** *(array)* – Seven-day history containing `{date, accuracy, reviews, correct}` for each day.
- **weak_cards** *(array)* – Up to five cards with the lowest accuracy, sorted ascending. Each entry contains the flashcard id, the prompt (`question`), deck metadata, accuracy ratio, total attempts, and correct count.

---

## Study Session Endpoints (Tracking Toggle)

These endpoints allow the frontend to opt-in to analytics tracking for a given deck. Tracking defaults to **off**.

### POST `/api/study/session/start`

```json
{
  "deck_id": 45,
  "track_reviews": false
}
```

Response:

```json
{
  "session_key": "123:45",
  "track_reviews": false,
  "deck_id": 45
}
```

### GET `/api/study/session/status?deck_id=45`

Response:

```json
{
  "track_reviews": false,
  "active": true
}
```

> **Note:** The current implementation keeps session state in memory. Replace `_study_sessions` with Redis or another store before deploying to production.

---

## Quiz Endpoints

All quiz responses exclude the correct answer until a submission is made to preserve challenge.

### POST `/api/quiz/generate`

```json
{
  "deck_ids": [1, 2],
  "total_questions": 10,
  "quiz_type": "multiple_choice",
  "time_limit_seconds": 600
}
```

Response (free tier example):

```json
{
  "quiz": {
    "id": 42,
    "user_id": 7,
    "quiz_type": "multiple_choice",
    "deck_ids": [1, 2],
    "total_questions": 10,
    "questions_answered": 0,
    "correct_answers": 0,
    "accuracy": 0.0,
    "time_limit_seconds": 600,
    "started_at": "2025-10-18T18:45:12.112345",
    "completed_at": null,
    "status": "in_progress"
  },
  "questions": [
    {
      "id": 301,
      "question_number": 1,
      "question_type": "multiple_choice",
      "question": "What organelle produces ATP?",
      "options": [
        "Mitochondria",
        "Nucleus",
        "Golgi apparatus",
        "Option 1"
      ]
    }
    // ...
  ],
  "usage": {
    "used": 3,
    "limit": 5,
    "remaining": 2,
    "week_key": "2025-W42"
  }
}
```

403 (limit reached):

```json
{
  "error": "Quiz limit reached",
  "message": "Upgrade to Premium for unlimited quizzes",
  "usage": {
    "used": 5,
    "limit": 5,
    "remaining": 0,
    "week_key": "2025-W42"
  },
  "upgrade_url": "/billing"
}
```

### POST `/api/quiz/<quiz_id>/answer`

```json
{
  "answer_id": 301,
  "user_answer": "Mitochondria",
  "time_spent_seconds": 9
}
```

Response:

```json
{
  "is_correct": true,
  "correct_answer": "Mitochondria",
  "feedback": "🎯 Excellent! You've got this! 🌟 Outstanding! You're mastering this material!",
  "current_score": 4,
  "total_answered": 5,
  "accuracy": 80.0
}
```

### POST `/api/quiz/<quiz_id>/complete`

```json
{}
```

Response:

```json
{
  "quiz_id": 42,
  "total_questions": 10,
  "correct_answers": 8,
  "accuracy": 80.0,
  "time_taken": 312.5,
  "weak_topics": [
    {
      "question": "Define photosynthesis",
      "correct_answer": "The process by which plants convert light into chemical energy.",
      "your_answer": "Creating sunlight",
      "deck_id": 2
    }
  ],
  "recommendation": "🎯 Excellent work! Focus on the questions you missed to polish your mastery."
}
```

### Error Responses

| Status | Body                                 | When                          |
|--------|--------------------------------------|-------------------------------|
| 401    | `{"error": "invalid token payload"}` | Missing/invalid JWT identity |
| 404    | `{"error": "User not found"}`        | User no longer exists         |

---

## Optional Endpoints

Current payloads give the frontend everything requested. If dedicated analytics endpoints become necessary, consider:

- `GET /stats/reviews?days=7` – return paginated review history.
- `GET /stats/weak-cards?deck_id=45&limit=10` – expose weak-card analysis independent of `/dashboard`.

These endpoints are **not** implemented yet; the dashboard response already embeds the relevant data.

---

## Testing Utilities

Use `pipenv run python test_stats_service.py` to seed sample review logs, exercise `StatsService`, and print a JSON report. The script auto-cleans its fixtures.

---

Document updated: 2025-10-16
