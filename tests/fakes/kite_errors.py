"""Look-alikes of kiteconnect.exceptions (5.0.1): same class names, hierarchy, default codes and `.code`.

broker/kite.py classifies errors by class name along the MRO (§5.8), so these drive the real classifier without
importing kiteconnect (whose package __init__ pulls in the twisted ticker).
"""

from __future__ import annotations


class KiteException(Exception):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GeneralException(KiteException):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message, code)


class TokenException(KiteException):
    def __init__(self, message: str, code: int = 403) -> None:
        super().__init__(message, code)


class PermissionException(KiteException):
    def __init__(self, message: str, code: int = 403) -> None:
        super().__init__(message, code)


class OrderException(KiteException):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message, code)


class InputException(KiteException):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message, code)


class DataException(KiteException):
    def __init__(self, message: str, code: int = 502) -> None:
        super().__init__(message, code)


class NetworkException(KiteException):
    def __init__(self, message: str, code: int = 503) -> None:
        super().__init__(message, code)
