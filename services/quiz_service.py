"""
Quiz generation and lifecycle helpers.
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, case

from config import db
from models import (
    Flashcard,
    Deck,
    ReviewLog,
    Quiz,
    QuizAnswer,
)

CORRECT_RESPONSES = [
    "🎯 Excellent! You've got this!",
    "✨ Perfect! Keep up the great work!",
    "💪 Nice job! You're mastering this!",
    "🌟 Fantastic! Your hard work is paying off!",
    "🚀 Outstanding! You're on fire!",
    "🔥 Brilliant answer!",
    "🙌 Nailed it!",
]

INCORRECT_RESPONSES = [
    "💡 Not quite! The answer is **{answer}**. You'll remember this next time!",
    "📚 Close! It's actually **{answer}**. Great effort though!",
    "🌱 Learning opportunity! The correct answer is **{answer}**. You're growing!",
    "🔁 Give it another go later – it's **{answer}**.",
    "🧠 Keep it up! The right answer is **{answer}**.",
]


class QuizService:
    """Service utilities for quiz creation and grading."""

    @staticmethod
    def get_card_accuracy(user_id: int, flashcard_id: int) -> float:
        """
        Calculate a flashcard accuracy for a user.
        Falls back to boolean was_correct when rating is absent.
        """
        rating_exists = hasattr(ReviewLog, "rating")
        correctness_clause = (
            case((ReviewLog.rating >= 3, 1), else_=0)
            if rating_exists
            else case((ReviewLog.was_correct == True, 1), else_=0)
        )

        row = db.session.query(
            func.count(ReviewLog.id).label("total"),
            func.sum(correctness_clause).label("correct"),
        ).filter(
            ReviewLog.user_id == user_id,
            ReviewLog.flashcard_id == flashcard_id,
        ).first()

        if not row or not row.total:
            return 0.0

        correct = row.correct or 0
        return float(correct) / float(row.total)

    @classmethod
    def categorize_cards(cls, user_id: int, deck_ids: Sequence[int]) -> Dict[str, List[Flashcard]]:
        """
        Categorize flashcards into weak/learning/mastered buckets based on accuracy.
        """
        if not deck_ids:
            return {"weak": [], "learning": [], "mastered": []}

        cards: List[Flashcard] = (
            Flashcard.query.filter(Flashcard.deck_id.in_(deck_ids)).all()
        )
        card_ids = [card.id for card in cards]
        if not card_ids:
            return {"weak": [], "learning": [], "mastered": []}

        rating_exists = hasattr(ReviewLog, "rating")
        correctness_clause = (
            case((ReviewLog.rating >= 3, 1), else_=0)
            if rating_exists
            else case((ReviewLog.was_correct == True, 1), else_=0)
        )

        accuracy_rows = db.session.query(
            ReviewLog.flashcard_id,
            func.count(ReviewLog.id).label("total"),
            func.sum(correctness_clause).label("correct"),
        ).filter(
            ReviewLog.user_id == user_id,
            ReviewLog.flashcard_id.in_(card_ids),
        ).group_by(ReviewLog.flashcard_id).all()

        accuracy_map = {
            row.flashcard_id: (row.correct or 0, row.total or 0)
            for row in accuracy_rows
        }

        buckets = {"weak": [], "learning": [], "mastered": []}
        for card in cards:
            correct, total = accuracy_map.get(card.id, (0, 0))
            accuracy = float(correct) / float(total) if total else 0.0

            if total == 0 or accuracy < 0.6:
                buckets["weak"].append(card)
            elif accuracy < 0.8:
                buckets["learning"].append(card)
            else:
                buckets["mastered"].append(card)

        return buckets

    @classmethod
    def select_quiz_cards(
        cls,
        user_id: int,
        deck_ids: Sequence[int],
        total_questions: int,
    ) -> List[Flashcard]:
        """
        Select cards using the 40/30/30 weak-learning-mastered distribution.
        Includes fallback redistribution when categories lack sufficient cards.
        """
        if total_questions <= 0:
            return []

        buckets = cls.categorize_cards(user_id, deck_ids)
        weak_cards = list(buckets["weak"])
        learning_cards = list(buckets["learning"])
        mastered_cards = list(buckets["mastered"])

        random.shuffle(weak_cards)
        random.shuffle(learning_cards)
        random.shuffle(mastered_cards)

        targets = {
            "weak": int(total_questions * 0.4),
            "learning": int(total_questions * 0.3),
        }
        assigned = targets["weak"] + targets["learning"]
        targets["mastered"] = max(0, total_questions - assigned)

        # distribute remainder to priority buckets
        remainder = total_questions - sum(targets.values())
        priority_order = ["weak", "learning", "mastered"]
        idx = 0
        while remainder > 0:
            targets[priority_order[idx % len(priority_order)]] += 1
            remainder -= 1
            idx += 1

        selections: List[Flashcard] = []

        def take_cards(source: List[Flashcard], desired: int) -> List[Flashcard]:
            taken = source[:desired]
            del source[:desired]
            return taken

        selections.extend(take_cards(weak_cards, targets["weak"]))
        selections.extend(take_cards(learning_cards, targets["learning"]))
        selections.extend(take_cards(mastered_cards, targets["mastered"]))

        deficit = total_questions - len(selections)
        if deficit > 0:
            leftovers = weak_cards + learning_cards + mastered_cards
            random.shuffle(leftovers)
            selections.extend(leftovers[:deficit])

        if len(selections) < total_questions:
            # Still short? pull from all flashcards in decks.
            remaining_pool = (
                Flashcard.query.filter(Flashcard.deck_id.in_(deck_ids))
                .filter(~Flashcard.id.in_([card.id for card in selections]))
                .all()
            )
            random.shuffle(remaining_pool)
            selections.extend(remaining_pool[: total_questions - len(selections)])

        selections = selections[:total_questions]
        random.shuffle(selections)
        return selections

    @staticmethod
    def generate_distractors(
        correct_answer: str,
        deck_id: Optional[int],
        flashcard_id: Optional[int],
        count: int = 3,
    ) -> List[str]:
        """
        Build realistic distractors from sibling flashcards.
        """
        if not deck_id:
            return [f"Option {i}" for i in range(1, count + 1)]

        query = Flashcard.query.filter(Flashcard.deck_id == deck_id)
        if flashcard_id:
            query = query.filter(Flashcard.id != flashcard_id)
        candidates = [card.back_text for card in query.limit(count * 5).all()]
        candidates = [text for text in candidates if text and text != correct_answer]
        random.shuffle(candidates)
        distractors = candidates[:count]

        if len(distractors) < count:
            missing = count - len(distractors)
            distractors.extend([f"Option {i}" for i in range(1, missing + 1)])

        return distractors

    @staticmethod
    def generate_feedback(
        is_correct: bool,
        correct_answer: str,
        score_percent: float,
    ) -> str:
        """
        Encouraging, growth-minded feedback.
        """
        message = random.choice(CORRECT_RESPONSES if is_correct else INCORRECT_RESPONSES)
        if not is_correct:
            message = message.format(answer=correct_answer)

        if score_percent >= 90:
            message += " 🌟 Outstanding! You're mastering this material!"
        elif score_percent >= 80:
            message += " 🎯 Excellent work! Keep reinforcing what you know."
        elif score_percent >= 70:
            message += " 💪 Good progress! Stay consistent and you'll get there."
        elif score_percent >= 60:
            message += " 📚 You're on track! Spend extra time reviewing tricky spots."
        else:
            message += " 🌱 Every attempt helps you grow! Keep practicing."

        return message

    @classmethod
    def generate_quiz(
        cls,
        user_id: int,
        deck_ids: Sequence[int],
        total_questions: int,
        quiz_type: str = "multiple_choice",
        time_limit_seconds: Optional[int] = None,
    ) -> Tuple[Quiz, List[dict]]:
        """
        Create a quiz and associated quiz answers, returning the quiz object and question payload.
        """
        cards = cls.select_quiz_cards(user_id, deck_ids, total_questions)
        if not cards:
            raise ValueError("No suitable flashcards available for the requested quiz.")

        quiz = Quiz(
            user_id=user_id,
            quiz_type=quiz_type,
            deck_ids=list(deck_ids),
            total_questions=len(cards),
            time_limit_seconds=time_limit_seconds,
        )
        db.session.add(quiz)
        db.session.flush()

        questions_payload: List[dict] = []
        for index, card in enumerate(cards, start=1):
            question_type = "multiple_choice"
            if quiz_type == "mixed":  # alternate styles for variety
                question_type = "written" if index % 2 == 0 else "multiple_choice"
            elif quiz_type == "written":
                question_type = "written"

            options = None
            if question_type == "multiple_choice":
                options = cls.generate_distractors(card.back_text, card.deck_id, card.id)
                options.append(card.back_text)
                random.shuffle(options)

            answer = QuizAnswer(
                quiz_id=quiz.id,
                flashcard_id=card.id,
                deck_id=card.deck_id,
                question_number=index,
                question_type=question_type,
                question_text=card.front_text,
                correct_answer=card.back_text,
                options=options,
            )
            db.session.add(answer)

            question_payload = {
                "id": None,  # will fill after flush
                "question_number": index,
                "question_type": question_type,
                "question": card.front_text,
                "options": options if options else [],
            }
            questions_payload.append(question_payload)

        db.session.flush()

        for payload, answer in zip(questions_payload, quiz.answers):
            payload["id"] = answer.id

        db.session.commit()
        return quiz, questions_payload

    @classmethod
    def submit_answer(
        cls,
        quiz_id: int,
        answer_id: int,
        user_answer: str,
        time_spent_seconds: Optional[int] = None,
    ) -> dict:
        """
        Record an answer and return immediate feedback.
        """
        quiz: Quiz | None = Quiz.query.filter_by(id=quiz_id).first()
        if not quiz:
            raise ValueError("Quiz not found.")

        answer: QuizAnswer | None = QuizAnswer.query.filter_by(id=answer_id, quiz_id=quiz_id).first()
        if not answer:
            raise ValueError("Answer not found for this quiz.")

        if answer.user_answer is not None:
            raise ValueError("Answer already submitted for this question.")

        user_answer_clean = (user_answer or "").strip()
        correct_clean = (answer.correct_answer or "").strip()
        is_correct = user_answer_clean.lower() == correct_clean.lower()

        answer.user_answer = user_answer
        answer.is_correct = is_correct
        answer.time_spent_seconds = time_spent_seconds
        answer.answered_at = datetime.utcnow()

        quiz.questions_answered = (quiz.questions_answered or 0) + 1
        if is_correct:
            quiz.correct_answers = (quiz.correct_answers or 0) + 1

        accuracy = (
            round((quiz.correct_answers or 0) / quiz.total_questions * 100, 2)
            if quiz.total_questions
            else 0.0
        )
        feedback = cls.generate_feedback(is_correct, answer.correct_answer, accuracy)
        answer.feedback = feedback

        db.session.commit()

        return {
            "is_correct": is_correct,
            "correct_answer": answer.correct_answer,
            "feedback": feedback,
            "current_score": quiz.correct_answers or 0,
            "total_answered": quiz.questions_answered or 0,
            "accuracy": accuracy,
        }

    @classmethod
    def complete_quiz(cls, quiz_id: int) -> dict:
        """
        Mark a quiz as completed and return a detailed summary.
        """
        quiz: Quiz | None = Quiz.query.filter_by(id=quiz_id).first()
        if not quiz:
            raise ValueError("Quiz not found.")

        if quiz.status == "completed":
            raise ValueError("Quiz already completed.")

        quiz.status = "completed"
        quiz.completed_at = datetime.utcnow()

        answers = QuizAnswer.query.filter_by(quiz_id=quiz_id).order_by(QuizAnswer.question_number).all()
        weak_topics = [
            {
                "question": ans.question_text,
                "correct_answer": ans.correct_answer,
                "your_answer": ans.user_answer,
                "deck_id": ans.deck_id,
            }
            for ans in answers
            if ans.is_correct is False
        ]

        accuracy = (
            round((quiz.correct_answers or 0) / quiz.total_questions * 100, 2)
            if quiz.total_questions
            else 0.0
        )

        recommendation = cls.get_recommendation(accuracy, len(weak_topics))
        time_taken = None
        if quiz.completed_at and quiz.started_at:
            delta = quiz.completed_at - quiz.started_at
            time_taken = round(delta.total_seconds(), 2)

        db.session.commit()

        return {
            "quiz_id": quiz.id,
            "total_questions": quiz.total_questions,
            "correct_answers": quiz.correct_answers,
            "accuracy": accuracy,
            "time_taken": time_taken,
            "weak_topics": weak_topics,
            "recommendation": recommendation,
        }

    @staticmethod
    def get_recommendation(accuracy: float, weak_count: int) -> str:
        """
        Provide study guidance based on final performance.
        """
        if accuracy >= 90:
            return "🌟 Outstanding! You've mastered this set—keep challenging yourself with new topics!"
        if accuracy >= 80:
            return "🎯 Excellent work! Focus on the questions you missed to polish your mastery."
        if accuracy >= 70:
            return "💪 Good progress! Review the tougher cards and take the quiz again soon."
        if accuracy >= 60:
            return "📚 You're on track! Spend extra time studying the cards you struggled with."
        return "🌱 Learning is a journey! Review thoroughly and try another quiz when you're ready."
