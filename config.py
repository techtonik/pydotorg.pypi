import ConfigParser
from urlparse import urlsplit, urlunsplit

class Config:
    ''' Read in the config and set up the vars with the correct type.
    '''
    def __init__(self, configfile):
        c = ConfigParser.ConfigParser()
        c.read(configfile)
        self.database_name = c.get('database', 'name')
        self.database_user = c.get('database', 'user')
        if c.has_option('database', 'driver'):
            self.database_driver = c.get('database', 'driver')
        else:
            self.database_driver = 'psycopg2'
        if c.has_option('database', 'password'):
            self.database_pw = c.get('database', 'password')
        else:
            self.database_pw = None
        self.database_files_dir = c.get('database', 'files_dir')
        self.database_docs_dir = c.get('database', 'docs_dir')
        if c.has_option('database', 'pubsubhubbub'):
            self.pubsubhubbub = c.get('database', 'pubsubhubbub')
        else:
            self.pubsubhubbub = None
        self.mailhost = c.get('webui', 'mailhost')
        self.adminemail = c.get('webui', 'adminemail')
        self.replyto = c.get('webui', 'replyto')
        self.url = c.get('webui', 'url')
        self.scheme_host = urlunsplit(urlsplit(self.url)[:2]+('','',''))
        self.orig_pydotorg = self.pydotorg = c.get('webui', 'pydotorg')
        self.simple_script = c.get('webui', 'simple_script')
        self.files_url = c.get('webui', 'files_url')
        self.rss_file = c.get('webui', 'rss_file')
        self.packages_rss_file = c.get('webui', 'packages_rss_file')
        self.debug_mode = c.get('webui', 'debug_mode')
        self.cheesecake_password = c.get('webui', 'cheesecake_password')
        self.key_dir = c.get('webui', 'key_dir')
        self.simple_sign_script = c.get('webui', 'simple_sign_script')
        if c.has_option('webui', 'sshkeys_update'):
            self.sshkeys_update = c.get('webui', 'sshkeys_update')
        else:
            self.sshkeys_update = None

        self.logfile = c.get('logging', 'file')
        self.logging_mailhost = c.get('logging', 'mailhost')
        self.fromaddr = c.get('logging', 'fromaddr')
        self.toaddrs = c.get('logging', 'toaddrs').split(',')

        self.sentry_dsn = c.get('sentry', 'dsn')

    def make_https(self):
        if self.url.startswith("http:"):
            self.url = "https"+self.url[4:]
            self.pydotorg = '/'

    def make_http(self):
        if self.url.startswith("https:"):
            self.url = "http"+self.url[5:]
            self.pydotorg = self.orig_pydotorg
