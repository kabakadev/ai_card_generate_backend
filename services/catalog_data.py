# services/catalog_data.py

CATALOG = {
    # --- Science ---
    "bio-cell-essentials": {
        "title": "Biology: Cell Essentials",
        "description": "Organelles, membranes, mitosis.",
        "subject": "Science",
        "category": "Biology",
        "difficulty": 1,
        "flashcards": [
            {"front_text": "What is the powerhouse of the cell?", "back_text": "Mitochondria"},
            {"front_text": "Function of ribosomes?", "back_text": "Protein synthesis"},
            {"front_text": "Stages of mitosis?", "back_text": "Prophase, Metaphase, Anaphase, Telophase"},
        ],
    },
    "chem-fundamentals": {
        "title": "Chemistry Fundamentals",
        "description": "Atoms, bonds, reactions.",
        "subject": "Science",
        "category": "Chemistry",
        "difficulty": 1,
        "flashcards": [
            {"front_text": "Atomic number represents?", "back_text": "Number of protons"},
            {"front_text": "NaCl is held together by what bond?", "back_text": "Ionic bond"},
            {"front_text": "Balance: H₂ + O₂ → ?", "back_text": "2H₂ + O₂ → 2H₂O"},
        ],
    },
    "physics-mechanics-1": {
        "title": "Physics: Mechanics I",
        "description": "Kinematics and Newton’s laws.",
        "subject": "Science",
        "category": "Physics",
        "difficulty": 2,
        "flashcards": [
            {"front_text": "Newton's First Law?", "back_text": "An object in motion stays in motion unless acted on"},
            {"front_text": "Formula for velocity?", "back_text": "v = d/t"},
            {"front_text": "F = ma. What does 'a' stand for?", "back_text": "Acceleration"},
        ],
    },

    # --- History ---
    "world-history-101": {
        "title": "World History 101",
        "description": "Ancient to early modern eras.",
        "subject": "History",
        "category": "World History",
        "difficulty": 1,
        "flashcards": [
            {"front_text": "Where did the first civilizations arise?", "back_text": "Mesopotamia"},
            {"front_text": "Who was the first emperor of Rome?", "back_text": "Augustus"},
            {"front_text": "What year did Columbus reach the Americas?", "back_text": "1492"},
        ],
    },
    "african-history-highlights": {
        "title": "African History Highlights",
        "description": "Key periods and leaders.",
        "subject": "History",
        "category": "African History",
        "difficulty": 1,
        "flashcards": [
            {"front_text": "Which kingdom built Great Zimbabwe?", "back_text": "The Shona"},
            {"front_text": "Who was Mansa Musa?", "back_text": "Emperor of Mali, richest man in history"},
            {"front_text": "What was the trans-Saharan trade?", "back_text": "Trade route linking West Africa and North Africa"},
        ],
    },
    "us-history-foundations": {
        "title": "US History Foundations",
        "description": "Colonial to Reconstruction.",
        "subject": "History",
        "category": "US History",
        "difficulty": 1,
        "flashcards": [
            {"front_text": "Who wrote the Declaration of Independence?", "back_text": "Thomas Jefferson"},
            {"front_text": "When was the US Constitution signed?", "back_text": "1787"},
            {"front_text": "What was the Civil War fought over?", "back_text": "Slavery and states' rights"},
        ],
    },

    # --- Languages ---
    "spanish-a1": {
        "title": "Spanish A1 Phrases",
        "description": "Essential beginner phrases in Spanish.",
        "subject": "Languages",
        "category": "Language",
        "difficulty": 1,
        "flashcards": [
            {"front_text": "Hola", "back_text": "Hello"},
            {"front_text": "Gracias", "back_text": "Thank you"},
            {"front_text": "¿Cómo estás?", "back_text": "How are you?"},
        ],
    },
}
