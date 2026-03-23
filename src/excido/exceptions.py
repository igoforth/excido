class NodeNotFoundException(Exception):
    """
    A specific type of exception that you can customize further.
    """

    def __init__(self, message: str = "My specific custom exception occurred", *args):
        self.message = message
        super().__init__(message, *args)
