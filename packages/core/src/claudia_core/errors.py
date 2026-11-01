class UserFacingError(Exception):
    """A failure whose message should reach the person in their own language.

    `key` names a message in the channel's catalog. Raw service errors never reach chat.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class SchedulerError(Exception):
    """An invalid scheduling request (bad time, bad cron, too many jobs).

    Caught at the tool layer and its message is read back to the agent verbatim, not to the
    person, so it is always in English regardless of the profile's language.
    """
