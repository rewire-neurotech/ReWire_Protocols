"""
Schema descriptions and onboarding category mapping.

Used by prompt_v45.py to inject a lightweight psychological context block
into the speech generation prompt. The model sees this as background
understanding — it shapes tone and format choice without being referenced
directly in the speech.

Schema descriptions written by Felix. Each entry names the wound (the
Early Maladaptive Schema) and the direction to push toward (ACTIVATE).
"""

# onboarding v24 pattern categories -> dominant schema name
ONBOARDING_CAT_TO_SCHEMA = {
    "critic":   "Punitiveness",
    "defect":   "Defectiveness / Shame",
    "perfect":  "Unrelenting Standards / Hypercriticalness",
    "first":    "Self-Sacrifice",
    "numb":     "Emotional Inhibition",
    "leave":    "Abandonment / Instability",
    "distance": "Mistrust / Abuse",
    "brace":    "Vulnerability to Harm or Illness",
    # "Something to grow" — user names the adaptive state directly
    "accept":   "Defectiveness / Shame",
    "belong":   "Social Isolation / Alienation",
    "trust":    "Dependence / Incompetence",
}

SCHEMA_DESCRIPTIONS = {
    # --- Disconnection & Rejection ---
    "Emotional Deprivation": (
        "No one ever tuned in closely enough to notice what they needed, "
        "so they stopped naming needs at all. "
        "ACTIVATE Emotional Fulfillment: being attended to by someone who "
        "wanted to, warmth arriving before it was asked for, being known "
        "in detail."
    ),
    "Abandonment / Instability": (
        "Everything close feels temporary, so closeness is spent watching "
        "for the pull-away. "
        "ACTIVATE Stable Attachment: ground that stays under them, "
        "steadiness that survives distance and silence, closeness without "
        "gripping."
    ),
    "Mistrust / Abuse": (
        "Everyone has an angle, so the guard never comes down; vigilance "
        "reads as competence and costs everything. "
        "ACTIVATE Basic Trust: the body's memory of unguarded minutes, "
        "safety in another person's presence, shoulders dropping by choice."
    ),
    "Defectiveness / Shame": (
        "Something at the core is wrong, and closeness is dangerous "
        "because closeness leads to exposure. "
        "ACTIVATE Self-Acceptance / Lovability: being seen all the way "
        "down and stayed with anyway, worth that survives disclosure."
    ),
    "Social Isolation / Alienation": (
        "Fundamentally different from everyone, watching the room from a "
        "half-step back even while inside it. "
        "ACTIVATE Social Belonging: the unspoken commonness of inner "
        "life, connection on their own terms and in the size of group "
        "they want."
    ),
    "Emotional Inhibition": (
        "Expression feels embarrassing or dangerous, so control runs "
        "constantly and stopped feeling like a choice. "
        "ACTIVATE Emotional Openness / Spontaneity: the throat opening, "
        "breath going deeper, a feeling allowed to move at full size."
    ),

    # --- Impaired Autonomy ---
    "Failure": (
        "Fundamentally less capable than peers; achievement gets "
        "reclassified as luck the moment it arrives. "
        "ACTIVATE Success / Competence: competence as an ordinary boring "
        "fact, capacity already exercised and already forgotten."
    ),
    "Dependence / Incompetence": (
        "Ordinary life feels like something requiring supervision; their "
        "own judgment is the last thing they'd trust. "
        "ACTIVATE Self-Reliance / Competence: their own decisions "
        "holding, handling something alone and noticing afterward that it "
        "got handled."
    ),
    "Vulnerability to Harm or Illness": (
        "Catastrophe is always about to arrive, and the bracing reads to "
        "them as prudence. "
        "ACTIVATE Basic Health and Safety: safety in the present body, "
        "the thousands of ordinary days already survived, breath that "
        "keeps going unsupervised."
    ),
    "Enmeshment / Undeveloped Self": (
        "No clear line between their life and a parent's or partner's; "
        "privacy feels like betrayal. "
        "ACTIVATE Healthy Boundaries / Developed Self: an inner life "
        "that belongs to them alone, separateness with the love intact."
    ),
    "Subjugation": (
        "Wanting something is dangerous — give in, or face anger and "
        "withdrawal. "
        "ACTIVATE Assertiveness / Self-Expression: preference as a "
        "simple fact, a true sentence leaving the mouth with the sky "
        "staying up."
    ),
    "Negativity / Pessimism": (
        "Good things are a setup, and relaxing the vigilance feels "
        "reckless. "
        "ACTIVATE Optimism / Hopefulness: permission to hold a good "
        "thing for its actual duration, the ordinariness of things "
        "working out."
    ),

    # --- Impaired Limits ---
    "Entitlement / Grandiosity": (
        "Limits register as insult; the specialness is exhausting and "
        "keeps everyone at a distance. "
        "ACTIVATE Empathic Consideration / Respect for Others: the "
        "relief of being one among many, another person's inner life "
        "becoming vivid. Give this listener no admiration anywhere in "
        "the speech."
    ),
    "Insufficient Self-Control / Self-Discipline": (
        "Frustration collapses into abandonment, and every unfinished "
        "thing becomes evidence. "
        "ACTIVATE Healthy Self-Control / Self-Discipline: staying "
        "through discomfort a few minutes longer, the pleasure on the "
        "far side of a boring middle."
    ),
    "Approval-Seeking / Recognition-Seeking": (
        "Worth is metered by attention received, so every room is a "
        "performance and every quiet hour a small death. "
        "ACTIVATE Self-Directedness: the value of a thing done "
        "unwatched, their own witness being enough. Never compliment "
        "this listener."
    ),

    # --- Exaggerated Expectations ---
    "Unrelenting Standards / Hypercriticalness": (
        "Second best reads as failure, and finishing produces the next "
        "item instead of relief. "
        "ACTIVATE Realistic Expectations: enough as a real state in the "
        "body, rest without penalty, something left unfinished on purpose."
    ),
    "Punitiveness": (
        "Mistakes require a sentence, and the sentence never expires. "
        "ACTIVATE Forgiveness / Self-Compassion: the reasons underneath "
        "the mistake becoming visible, speaking to themselves in the "
        "voice they'd use for someone they love."
    ),
    "Self-Sacrifice": (
        "Goodness is measured by being needed; their own needs are "
        "unlisted and receiving feels like a debt. "
        "ACTIVATE Healthy Self-Interest / Self-Care: being received, "
        "reciprocity, their needs weighted equally to everyone else's."
    ),
}
