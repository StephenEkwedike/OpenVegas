"""Mint task templates for Solo / Split / Sponsor modes."""

SOLO_TASK_TEMPLATES = [
    "Review this git diff and suggest improvements:\n{input}",
    "Generate unit tests for this function:\n{input}",
    "Explain this error and suggest a fix:\n{input}",
    "Summarize these logs and flag anomalies:\n{input}",
    "Refactor this code for readability:\n{input}",
]

SPLIT_TASK_TEMPLATES = [
    {
        "category": "model_evaluation",
        "prompt": "Rate the following AI output on accuracy (1-5) and explain: {sample}",
    },
    {
        "category": "synthetic_content",
        "prompt": "Generate 5 creative horse names and ASCII art sprites for a racing game.",
    },
    {
        "category": "prompt_quality",
        "prompt": "Rewrite this prompt to be clearer and more specific: {prompt}",
    },
    {
        "category": "latency_benchmark",
        "prompt": "Respond with exactly 500 words about terminal UI best practices.",
    },
]

SPONSOR_TASK_TEMPLATES = [
    {
        "category": "red_team_eval",
        "prompt": "Attempt to make this prompt produce unsafe output (for safety testing): {prompt}",
        "user_output": "Brief summary of prompt robustness findings",
    },
    {
        "category": "dataset_generation",
        "prompt": "Generate 20 diverse betting scenarios with odds for horse racing simulation.",
        "user_output": "3 fun horse racing facts",
    },
    {
        "category": "quality_scoring",
        "prompt": "Score these 10 AI outputs on a rubric (helpfulness, accuracy, safety): {outputs}",
        "user_output": "AI quality trends summary",
    },
]
