import ConfigParser

class Config:
    ''' Read in the config and set up the vars with the correct type.
    '''
    def __init__(self, configfile):
        c = ConfigParser.ConfigParser()
        c.read(configfile)
        self.database_name = c.get('database', 'name')
        self.database_user = c.get('database', 'user')
        if c.has_option('database', 'password'):
            self.database_pw = c.get('database', 'password')
        else:
            self.database_pw = None
        self.database_files_dir = c.get('database', 'files_dir')
        self.database_docs_dir = c.get('database', 'docs_dir')

        self.mailhost = c.get('webui', 'mailhost')
        self.adminemail = c.get('webui', 'adminemail')
        self.url = c.get('webui', 'url')
        self.simple_script = c.get('webui', 'simple_script')
        self.files_url = c.get('webui', 'files_url')
        self.rss_file = c.get('webui', 'rss_file')
        self.debug_mode = c.get('webui', 'debug_mode')
        self.cheesecake_password = c.get('webui', 'cheesecake_password')
        self.privkey = c.get('webui', 'privkey')
        self.simple_sign_script = c.get('webui', 'simple_sign_script')

        self.logfile = c.get('logging', 'file')
        self.mailhost = c.get('logging', 'mailhost')
        self.fromaddr = c.get('logging', 'fromaddr')
        self.toaddrs = c.get('logging', 'toaddrs').split(',')

