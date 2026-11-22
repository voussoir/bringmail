from voussoirkit import stringtools


class ErrorTypeAdder(type):
    '''
    During definition, the Exception class will automatically receive a class
    attribute called `error_type` which is just the class's name as a string
    in the loudsnake casing style. NoSuchAccount -> NO_SUCH_ACCOUNT.

    This is used for serialization of the exception object and should
    basically act as a status code when displaying the error to the user.
    '''
    def __init__(cls, name, bases, clsdict):
        type.__init__(cls, name, bases, clsdict)
        cls.error_type = stringtools.pascal_to_loudsnakes(name)


class BringException(Exception, metaclass=ErrorTypeAdder):
    '''
    Base type for all of the BringMail exceptions.

    Subtypes should have a class attribute `error_message`. The error message
    may contain {format} strings which will be formatted using the
    Exception's constructor arguments.
    '''
    error_message = ''

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.given_args = args
        self.given_kwargs = kwargs
        self.error_message = self.error_message.format(*args, **kwargs)
        self.args = (self.error_message, args, kwargs)

    def __str__(self):
        return f'{self.error_type}: {self.error_message}'

    def jsonify(self):
        j = {
            'type': 'error',
            'error_type': self.error_type,
            'error_message': self.error_message,
        }
        return j


# NO SUCH ##########################################################################################

class NoSuch(BringException):
    pass


class NoSuchAccount(NoSuch):
    error_message = 'Account "{}" does not exist.'


class NoSuchFolder(NoSuch):
    error_message = 'Folder "{}" does not exist.'


class NoSuchMessage(NoSuch):
    error_message = 'Message "{}" does not exist.'


class NoSuchPart(NoSuch):
    error_message = 'Message part "{}" does not exist.'


# ACCOUNTS / FOLDERS ###############################################################################

class InvalidAccount(BringException):
    error_message = '{}'


class DuplicateAccount(InvalidAccount):
    error_message = 'An account for {username} at {host}:{port} already exists.'


class InvalidFolder(BringException):
    error_message = '{}'


class FolderNotSelectable(InvalidFolder):
    error_message = 'Folder "{}" can not be selected.'


class TrashNotFound(BringException):
    error_message = 'This account has no trash folder; delete is unavailable.'


# MESSAGES #########################################################################################

class MessageNotCached(BringException):
    error_message = 'The body of message "{}" has not been downloaded yet.'


class PendingConflict(BringException):
    '''
    A queued operation was abandoned because the server state no longer
    matches what the user acted on.
    '''
    error_message = '{}'


class NothingToRestore(BringException):
    error_message = 'This message has no deleted state to restore.'


class BadEmail(BringException):
    error_message = 'Could not parse email "{}".'


# MAIL SERVER ######################################################################################

class MailServerError(BringException):
    error_message = '{}'


class MailServerConnectionError(MailServerError):
    error_message = 'Could not connect to the mail server: {}'


class MailServerLoginError(MailServerError):
    error_message = 'Could not log in: {}'


class MailServerActionError(MailServerError):
    error_message = 'The mail server refused the action: {}'


class IdleUnsupported(MailServerError):
    error_message = 'The mail server does not advertise the IMAP IDLE capability.'


class IdleDisconnected(MailServerConnectionError):
    '''
    The server closed an IMAP IDLE connection. The watcher reconnects, so this
    is an expected part of the IDLE lifecycle rather than a failure that needs
    attention.
    '''
    error_message = '{}'


# DATABASE #########################################################################################

class BadDataDirectory(BringException):
    '''
    Raised by BringMailDB.__init__ if the requested data_directory is invalid.
    '''
    error_message = 'Bad data directory "{}"'


class DatabaseOutOfDate(BringException):
    '''
    Raised by BringMailDB.__init__ if the user's database is behind.
    '''
    error_message = '''
Database is out of date. {existing} should be {new}.
Please stop BringMail and run utilities/database_upgrader.py.
'''.strip()


class DatabaseTooNew(BringException):
    '''
    Raised by BringMailDB.__init__ if the database was created by a newer copy
    of BringMail than the one that is running.
    '''
    error_message = '''
Database version {existing} is newer than this copy of BringMail supports ({new}).
Use a newer copy of BringMail or restore a backup from before the upgrade.
'''.strip()


class DatabaseUpgradeError(BringException):
    '''
    Raised by bringmail.upgrades when a migration cannot be applied safely.
    '''
    error_message = '{}'


class SQLiteTooOld(BringException):
    '''
    Raised by BringMailDB.__init__ if the Python build's SQLite is older than
    constants.MINIMUM_SQLITE.
    '''
    error_message = '''
BringMail requires SQLite {required} or newer, but this Python is using SQLite {installed}.
Install a newer Python (3.11 or newer is a safe choice) and run BringMail again.
'''.strip()


class NoClosestBringMailDB(BringException):
    '''
    For calls to BringMailDB.closest_bringmaildb where none exists between cwd
    and drive root.
    '''
    error_message = 'There is no BringMail database in "{}" or its parents.'


class NotExclusive(BringException):
    '''
    For when two or more mutually exclusive actions have been requested.
    '''
    error_message = 'One and only one of {} must be passed.'


# GENERAL ##########################################################################################

class NotSupported(BringException):
    error_message = '{}'
