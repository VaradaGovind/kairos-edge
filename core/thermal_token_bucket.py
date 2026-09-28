class ThermalTokenBucket:
    """
    L2-C3: Thermal token bucket simulator/proxy for UMA bandwidth throttling.
    """
    def __init__(self, max_tokens: float, replenish_rate_hz: float):
        self.max_tokens = max_tokens
        self.tokens = max_tokens
        self.replenish_rate_hz = replenish_rate_hz

    def step(self, dt_s: float, thermal_headroom_c: float) -> None:
        """
        Update token bucket based on thermal headroom.
        """
        effective_rate = self.replenish_rate_hz
        if thermal_headroom_c <= 0.0:
            effective_rate = 0.0 # Full throttle
        elif thermal_headroom_c < 5.0:
            effective_rate *= 0.1 # Severe throttle
            
        self.tokens = min(self.max_tokens, self.tokens + effective_rate * dt_s)

    def consume(self, amount: float) -> bool:
        """
        Attempt to consume bandwidth tokens. Returns True if successful.
        """
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False
