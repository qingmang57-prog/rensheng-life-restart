"""Billing errors. Every error carries machine-readable details for the HTTP layer."""


class CreditsError(Exception):
    """Base class for all billing failures."""

    code = 'credits_error'

    def __init__(self, message, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self):
        return {'error': self.code, 'message': self.message, **self.details}


class ValidationError(CreditsError):
    code = 'validation_error'


class AccountError(CreditsError):
    code = 'account_error'


class AccountNotFound(AccountError):
    code = 'account_not_found'


class AccountDisabled(AccountError):
    code = 'account_disabled'


class UnknownAction(CreditsError):
    code = 'unknown_action'


class InsufficientCredits(CreditsError):
    code = 'insufficient_credits'


class IdempotencyConflict(CreditsError):
    code = 'idempotency_conflict'


class InvalidState(CreditsError):
    """Illegal status transition (e.g. settle a released hold)."""

    code = 'invalid_state'


class TicketError(CreditsError):
    code = 'ticket_error'


class TicketNotFound(TicketError):
    code = 'ticket_not_found'


class TicketConflict(TicketError):
    code = 'ticket_conflict'


class InvalidTicketSignature(TicketError):
    code = 'ticket_signature_invalid'


class TicketExpired(TicketError):
    code = 'ticket_expired'


class InvalidResult(CreditsError):
    code = 'invalid_result'
