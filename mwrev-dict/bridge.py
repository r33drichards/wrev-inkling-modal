"""Temporary bridge: new deliveries live in Dict; existing Neon jobs drain."""
class BridgeIngress:
    def __init__(self, legacy, current):
        self.legacy = legacy
        self.current = current

    async def persist(self, intent):
        found = await self.legacy.attach_legacy_delivery(intent)
        if found is not None:
            return found
        return await self.current.persist(intent)

    async def dispatch(self, id, spawn):
        return await (self.legacy if type(id) is int else self.current).dispatch(id, spawn)
