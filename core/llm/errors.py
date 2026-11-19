class LLMError(Exception):
    """Base class for LLM-layer failures."""


class BudgetExceeded(LLMError):
    def __init__(self, spent_eur: float, budget_eur: float) -> None:
        super().__init__(f"monthly budget exceeded: {spent_eur:.4f} >= {budget_eur:.2f} EUR")
        self.spent_eur = spent_eur
        self.budget_eur = budget_eur
