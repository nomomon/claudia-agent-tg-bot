class UserFacingError(Exception):
    """A failure whose message should reach the person in their own language.

    `key` names a message in the channel's catalog. Raw service errors never reach chat.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)
