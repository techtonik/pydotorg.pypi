# system imports
import sys, os, urllib, cStringIO, traceback, cgi, binascii, gzip
import time, random, smtplib, base64, email, types, urlparse
import re, zipfile, shutil, Cookie, subprocess, hashlib
import logging
from zope.pagetemplate.pagetemplatefile import PageTemplateFile
from distutils.util import rfc822_escape
from distutils2.metadata import Metadata
from xml.etree import cElementTree

try:
    import json
except ImportError:
    import simplejson as json
try:
    import psycopg2
    OperationalError = psycopg2.OperationalError
except ImportError:
    class OperationalError(Exception):
        pass

# Importing M2Crypto patches urllib; don't let them do that
orig = urllib.URLopener.open_https.im_func
from M2Crypto import EVP, DSA
urllib.URLopener.open_https = orig

# OpenId provider imports
OPENID_FILESTORE = '/tmp/openid-filestore'

from openid.server import server as OpenIDServer

# Raven for error reporting
import raven

# local imports
import store, config, versionpredicate, verify_filetype, rpc
import MailingLogger, openid2rp, gae
from mini_pkg_resources import safe_name
from description_utils import extractPackageReadme
import oauth

esc = cgi.escape
esq = lambda x: cgi.escape(x, True)

def enumerate(sequence):
    return [(i, sequence[i]) for i in range(len(sequence))]

safe_filenames = re.compile(r'.+?\.(exe|tar\.gz|bz2|rpm|deb|zip|tgz|egg|dmg|msi)$', re.I)
safe_username = re.compile(r'^[A-Za-z0-9._]+$')
safe_email = re.compile(r'^[a-zA-Z0-9._+@-]+$')
botre = re.compile(r'^$|brains|yeti|myie2|findlinks|ia_archiver|psycheclone|badass|crawler|slurp|spider|bot|scooter|infoseek|looksmart|jeeves', re.I)

class NotFound(Exception):
    pass
class Unauthorised(Exception):
    pass
class Forbidden(Exception):
    pass
class Redirect(Exception):
    pass
class RedirectFound(Exception):# 302
    pass
class RedirectTemporary(Exception): # 307
    pass
class FormError(Exception):
    pass
class OpenIDError(Exception):
    pass

class MultipleReleases(Exception):
    def __init__(self, releases):
        self.releases = releases

__version__ = '1.1'

providers = (('Google', 'https://www.google.com/favicon.ico', 'https://www.google.com/accounts/o8/id'),
             ('myOpenID', 'https://www.myopenid.com/favicon.ico', 'https://www.myopenid.com/'),
             ('Launchpad', 'https://launchpad.net/@@/launchpad.png', 'https://login.launchpad.net/')
             )

# email sent to user indicating how they should complete their registration
rego_message = '''Subject: Complete your PyPI registration
From: %(admin)s
To: %(email)s

To complete your registration of the user "%(name)s" with the python module
index, please visit the following URL:

  %(url)s?:action=user&otk=%(otk)s

'''

# password change request email
password_change_message = '''Subject: PyPI password change request
From: %(admin)s
To: %(email)s

Someone, perhaps you, has requested that the password be changed for your
username, "%(name)s". If you wish to proceed with the change, please follow
the link below:

  %(url)s?:action=password_reset&email=%(email)s

You should then receive another email with the new password.

'''

# password reset email - indicates what the password is now
password_message = '''Subject: PyPI password has been reset
From: %(admin)s
To: %(email)s

Your login is: %(name)s
Your password is now: %(password)s
'''

_prov = '<p>You may also login or register using <a href="%(url_path)s?:action=openid">OpenID</a>'
for title, favicon, login in providers:
    _prov += '''
    <a href="%s"><img src="%s" title="%s"/></a>
    ''' %  (login, favicon, title)
_prov += "</p>"
unauth_message = '''
<p>If you are a new user, <a href="%(url_path)s?:action=register_form">please
register</a>.</p>
<p>If you have forgotten your password, you can have it
<a href="%(url_path)s?:action=forgotten_password_form">reset for you</a>.</p>
''' + _prov

chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

class Provider:
    def __init__(self, name, favicon, url):
        self.name = self.title = name
        self.favicon = favicon
        self.url = url

class _PyPiPageTemplate(PageTemplateFile):
    def pt_getContext(self, args=(), options={}, **kw):
        """Add our data into ZPT's defaults"""
        rval = PageTemplateFile.pt_getContext(self, args=args)
        options.update(rval)
        return options

cache_templates = True
if cache_templates:
    template_cache = {}
    def PyPiPageTemplate(file, dir):
        try:
            return template_cache[(file, dir)]
        except KeyError:
            t = _PyPiPageTemplate(file, dir)
            template_cache[(file, dir)] = t
            return t
else:
    PyPiPageTemplate = _PyPiPageTemplate

class FileUpload:
    pass

# poor man's markup heuristics so we don't have to use <PRE>,
# for when rst didn't work on the text...
br_patt = re.compile(" *\r?\n\r?(?= +)")
p_patt = re.compile(" *\r?\n(\r?\n)+")
def newline_to_br(text):
    text = re.sub(br_patt, "<BR/>", text)
    return re.sub(p_patt, "\n<P>\n", text)

def path2str(path):
    return " :: ".join(path)

def str2path(s):
    return [ node.strip() for node in s.split("::") ]


def transmute(field):
    if hasattr(field, 'filename') and field.filename:
        v = FileUpload()
        v.filename = field.filename
        v.value = field.value
        v.type = field.type
    else:
        v = field.value.decode('utf-8')
    return v

def decode_form(form):
    d = {}
    for k in form.keys():
        v = form[k]
        if isinstance(v, list):
            d[k] = [transmute(i) for i in v]
        else:
            d[k] = transmute(v)
    return d

class WebUI:
    ''' Handle a request as defined by the "env" parameter. "handler" gives
        access to the user via rfile and wfile, and a few convenience
        functions (see pypi).

        The handling of a request goes as follows:
        1. open the database
        2. see if the request is supplied with authentication information
        3. perform the action defined by :action ("home" if none is supplied)
        4a. handle exceptions sanely, including special ones like NotFound,
            Unauthorised, Redirect and FormError, or
        4b. commit changes to the database
        5. close the database to finish off

    '''
    def __init__(self, handler, env):
        self.handler = handler
        self.config = handler.config
        self.wfile = handler.wfile
        self.sentry_client = raven.Client(self.config.sentry_dsn)
        self.env = env
        self.nav_current = None
        self.privkey = None
        self.username = None
        self.authenticated = False # was a password or a valid cookie passed?
        self.loggedin = False      # was a valid cookie sent?
        self.usercookie = None
        self.failed = None # error message if initialization already produced a failure

        # XMLRPC request or not?
        if self.env.get('CONTENT_TYPE') != 'text/xml':
            fs = cgi.FieldStorage(fp=handler.rfile, environ=env)
            try:
                self.form = decode_form(fs)
            except UnicodeDecodeError:
                self.failed = "Form data is not correctly encoded in UTF-8"
        else:
            self.form = None

        if env.get("HTTPS") == 'on':
            self.config.make_https()
        else:
            self.config.make_http()

        (protocol, machine, path, x, x, x) = urlparse.urlparse(self.config.url)
        self.url_machine = '%s://%s'%(protocol, machine)
        self.url_path = path

        # configure logging
        if self.config.logfile or self.config.mailhost:
            root = logging.getLogger()
            if self.config.debug_mode == 'yes':
                root.setLevel(logging.DEBUG)
                hdlr = logging.StreamHandler()
                hdlr.setFormatter(logging.Formatter(
                    fmt='%(asctime)s:%(levelname)s %(message)s',
                    datefmt='%H:%M:%S'))
                root.addHandler(hdlr)
                logging.info("--- Running PyPI in DEBUG mode ---")
            if self.config.logfile:
                hdlr = logging.FileHandler(self.config.logfile)
                formatter = logging.Formatter(
                    '%(asctime)s %(name)s:%(levelname)s %(message)s')
                hdlr.setFormatter(formatter)
                root.addHandler(hdlr)
            if self.config.logging_mailhost:
                hdlr = MailingLogger.MailingLogger(self.config.logging_mailhost,
                    self.config.fromaddr, self.config.toaddrs,
                    '[PyPI] %(line)s', False, flood_level=10)
                root.addHandler(hdlr)

    def run(self):
        ''' Run the request, handling all uncaught errors and finishing off
            cleanly.
        '''
        if self.failed:
            # failed during initialization
            self.fail(self.failed)
            return
        self.store = store.Store(self.config)
        try:
            try:
                self.store.get_cursor() # make sure we can connect
                op_endpoint = "%s?:action=openid_endpoint" % (self.config.url,)
                self.oid_server = OpenIDServer.Server(self.store.oid_store(), op_endpoint=op_endpoint)
                self.inner_run()
            except NotFound, err:
                self.fail('Not Found (%s)' % err, code=404)
            except Unauthorised, message:
                message = str(message)
                if not message:
                    message = 'You must login to access this feature'
                msg = unauth_message%self.__dict__
                self.fail(message, code=401, heading='Login required',
                    content=msg, headers={'WWW-Authenticate':
                    'Basic realm="pypi"'})
            except Forbidden, message:
                message = str(message)
                self.fail(message, code=403, heading='Forbidden')
            except Redirect, e:
                self.handler.send_response(301, 'Moved Permanently')
                self.handler.send_header('Location', e.args[0])
                self.handler.end_headers()
            except RedirectFound, e:
                self.handler.send_response(302, 'Found')
                self.handler.send_header('Location', e.args[0])
                self.handler.end_headers()
            except RedirectTemporary, e:
                # ask browser not to cache this redirect
                self.handler.send_response(307, 'Temporary Redirect')
                self.handler.send_header('Location', e.args[0])
                self.handler.end_headers()
            except FormError, message:
                message = str(message)
                self.fail(message, code=400, heading='Error processing form')
            except OpenIDError, message:
                message = str(message)
                self.fail(message, code=400, heading='Error processing OpenID request')
            except IOError, error:
                # ignore broken pipe errors (client vanished on us)
                if error.errno != 32: raise
            except OperationalError, message:
                # clean things up
                self.store.force_close()
                message = str(message)
                self.fail('Please try again later.\n<!-- %s -->'%message,
                    code=500, heading='Database connection failed')
            except:
                exc, value, tb = sys.exc_info()

                # attempt to send all the exceptions to Raven
                try:
                    self.sentry_client.captureException()
                except Exception:
                    # sentry broke so just email the exception like old times
                    if ('connection limit exceeded for non-superusers'
                            not in str(value)):
                        logging.exception('Internal Error\n----\n%s\n----\n'%(
                            '\n'.join(['%s: %s'%x for x in self.env.items()])))

                if self.config.debug_mode == 'yes':
                    s = cStringIO.StringIO()
                    traceback.print_exc(None, s)
                    s = cgi.escape(s.getvalue())
                    self.fail('Internal Server Error', code=500,
                        heading='Error...', content='%s'%s)
                else:
                    s = '%s: %s'%(exc, value)
                    self.fail("There's been a problem with your request",
                        code=500, heading='Error...', content='%s'%s)
        finally:
            self.store.close()

    # these are inserted at the top of the standard template if set
    error_message = None
    ok_message = None

    def write_plain(self, payload):
        self.handler.send_response(200)
        self.handler.send_header("Content-type", 'text/plain')
        self.handler.send_header("Content-length", str(len(payload)))
        self.handler.end_headers()
        self.handler.wfile.write(payload)

    def write_template(self, filename, headers={}, **options):
        context = {}
        options.setdefault('norobots', False)
        options.setdefault('keywords', 'python programming language object'
            ' oriented web free source package index download software')
        options.setdefault('description', 'The Python Package Index is a'
            ' repository of software for the Python programming language.')
        options['providers'] = self.get_providers()
        context['data'] = options
        context['app'] = self
        fpi = self.config.url+self.env.get('PATH_INFO',"")
        try:
            options['FULL_PATH_INFO'] = fpi.decode("utf-8")
        except UnicodeError:
            raise NotFound, fpi + ' is not utf-8 encoded'

        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        context['standard_template'] = PyPiPageTemplate(
            "standard_template.pt", template_dir)
        template = PyPiPageTemplate(filename, template_dir)
        content = template(**context)

        # dynamic insertion of CSRF token into FORMs
        if '"POST"' in content and self.authenticated:
            token = '<input type="hidden" name="CSRFToken" value="%s">' % (
                    self.store.get_token(self.username),)
            temp = content.split('\n')
            edit = ((i, l) for i, l in enumerate(content.split('\n')) if
                    '"POST"' in l)
            try:
                for index, line in edit:
                    while not line.endswith('>'):
                        index += 1
                        line = temp[index]
                    # count spaces to align entry nicely
                    spaces = len(line.lstrip()) - len(line)
                    temp[index] = "\n".join((line, ' ' * spaces + token))
                content = '\n'.join(temp)
            except IndexError:
                # this should not happen with correct HTML syntax
                # the try is 'just in case someone does something stupid'
                pass

        self.handler.send_response(200, 'OK')
        if 'content-type' in options:
            self.handler.set_content_type(options['content-type'])
        else:
            self.handler.set_content_type('text/html; charset=utf-8')
        if self.usercookie:
            self.handler.send_header('Set-Cookie',
                'pypi=%s;path=%s/' % (self.usercookie, self.url_machine))
        for k,v in headers.items():
            self.handler.send_header(k, v)
        self.handler.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def fail(self, message, title="Python Package Index", code=400,
            heading=None, headers={}, content=''):
        ''' Indicate to the user that something has failed.
        '''
        self.handler.send_response(code, message)
        if '<' in content and '>' in content:
            html = True
            self.handler.set_content_type('text/html; charset=utf-8')
        else:
            html = False
            self.handler.set_content_type('text/plain; charset=utf-8')

        for k,v in headers.items():
            self.handler.send_header(k, v)
        self.handler.end_headers()

        if heading:
            if html:
                self.wfile.write('<strong>' + heading +
                    '</strong><br /><br />\n\n')
            else:
                self.wfile.write(heading + '\n\n')
        self.wfile.write(message)
        if html: self.wfile.write('<br /><br />\n')
        else: self.wfile.write('\n\n')
        self.wfile.write(content)

    def link_action(self, action_name=None, **vars):
        if action_name:
            vars[':action'] = action_name
        l = []
        for k,v in vars.items():
            l.append('%s=%s'%(urllib.quote(k.encode('utf-8')),
                urllib.quote(v.encode('utf-8'))))
        return self.url_path + '?' + '&'.join(l)

    navlinks = (
        ('browse', 'Browse packages'),
        ('submit_form', 'Package submission'),
        ('list_classifiers', 'List trove classifiers'),
        ('index', 'List packages'),
        ('rss', 'RSS (latest 40 updates)'),
        ('packages_rss', 'RSS (newest 40 packages)'),
        ('role_form', 'Admin'),
    )
    def navlinks_html(self):
        links = []
        for action_name, desc in self.navlinks:
            desc = desc.replace(' ', '&nbsp;')
            if action_name == 'role_form' and (
                not self.username or not self.store.has_role('Admin', '')):
                continue

            cssclass = ''
            if action_name == self.nav_current:
                cssclass = 'selected'
            links.append('<li class="%s"><a class="%s" href="%s">%s</a></li>' %
                         (cssclass, cssclass, self.link_action(action_name), desc))
        return links


    def inner_run(self):
        ''' Figure out what the request is, and farm off to the appropriate
            handler.
        '''
        # See if this is the "simple" pages and signatures
        script_name = self.env.get('SCRIPT_NAME')
        if script_name and script_name == self.config.simple_script:
            return self.run_simple()
        if script_name and script_name == self.config.simple_sign_script:
            return self.run_simple_sign()
        if script_name == '/mirrors':
            return self.mirrors()
        if script_name == '/daytime':
            return self.daytime()
        if script_name == '/id':
            return self.run_id()

        # see if the user has provided a username/password
        auth = self.env.get('HTTP_CGI_AUTHORIZATION', '').strip()
        if auth:
            authtype, auth = auth.split(None, 1) # OAuth has many more parameters
            if authtype.lower() == 'basic':
                try:
                    un, pw = base64.decodestring(auth).split(':')
                except (binascii.Error, ValueError):
                    # Invalid base64, or not exactly one colon
                    un = pw = ''
                if self.store.has_user(un):
                    pw = hashlib.sha1(pw).hexdigest()
                    user = self.store.get_user(un)
                    if pw != user['password']:
                        raise Unauthorised, 'Incorrect password'
                    self.username = un
                    self.authenticated = True
                    last_login = user['last_login']
                    # Only update last_login every minute
                    update_last_login = not last_login or (time.time()-time.mktime(last_login.timetuple()) > 60)
                    self.store.set_user(un, self.env['REMOTE_ADDR'], update_last_login)
        else:
            un = self.env.get('SSH_USER', '')
            if un and self.store.has_user(un):
                user = self.store.get_user(un)
                self.username = un
                self.authenticated = self.loggedin = True
                last_login = user['last_login']
                # Only update last_login every minute
                update_last_login = not last_login or (time.time()-time.mktime(last_login.timetuple()) > 60)
                self.store.set_user(un, self.env['REMOTE_ADDR'], update_last_login)

        # on logout, we set the cookie to "logged_out"
        self.cookie = Cookie.SimpleCookie(self.env.get('HTTP_COOKIE', ''))
        try:
            self.usercookie = self.cookie['pypi'].value
        except KeyError:
            self.usercookie = None

        name = self.store.find_user_by_cookie(self.usercookie)
        if name:
            self.loggedin = True
            self.authenticated = True # implied by loggedin
            self.username = name
            # no login time update, since looking for the
            # cookie did that already
            self.store.set_user(name, self.env['REMOTE_ADDR'], False)

        # Commit all user-related changes made up to here
        if self.username:
            self.store.commit()

        # Now we have a username try running OAuth if necessary
        if script_name == '/oauth':
            return self.run_oauth()

        if self.env.get('CONTENT_TYPE') == 'text/xml':
            self.xmlrpc()
            return

        # now handle the request
        path = self.env.get('PATH_INFO', '')
        if self.form.has_key(':action'):
            action = self.form[':action']
            if isinstance(action, list):
                raise RuntimeError("Multiple actions: %r" % action)
        elif path:
            # Split into path items, drop leading slash
            try:
                items = path.decode('utf-8').split('/')[1:]
            except UnicodeError:
                raise NotFound(path + " is not UTF-8 encoded")
            action = None
            if path == '/':
                self.form['name'] = ''
                action = 'index'
            elif len(items) >= 1:
                self.form['name'] = items[0]
                action = 'display'
            if len(items) >= 2 and items[1]:
                self.form['version'] = items[1]
                action = 'display'
            if len(items) == 3 and items[2]:
                action = self.form[':action'] = items[2]
            if not action:
                raise NotFound
        else:
            action = 'home'

        if self.form.get('version') in ('doap', 'json'):
            action, self.form['version'] = self.form['version'], None

        # make sure the user has permission
        if action in ('submit', ):
            if not self.authenticated:
                raise Unauthorised
            if self.store.get_otk(self.username):
                raise Unauthorised, "Incomplete registration; check your email"

        # handle the action
        if action in '''debug home browse rss index search submit doap
        display_pkginfo submit_pkg_info remove_pkg pkg_edit verify submit_form
        display register_form user_form forgotten_password_form user
        password_reset role role_form list_classifiers login logout files
        file_upload show_md5 doc_upload claim openid openid_return dropid
        clear_auth addkey delkey lasthour json gae_file about delete_user
        rss_regen openid_endpoint openid_decide_post packages_rss
        exception'''.split():
            getattr(self, action)()
        else:
            #raise NotFound, 'Unknown action %s' % action
            raise NotFound

        if action in 'submit submit_pkg_info pkg_edit remove_pkg'.split():
            self.rss_regen()

        # commit any database changes
        self.store.commit()

    def debug(self):
        self.fail('Debug info', code=200, content=str(self.env))

    def exception(self):
        FAIL

    def xmlrpc(self):
        rpc.handle_request(self)

    def simple_body(self, path):
        urls = self.store.get_package_urls(path, relative="../../packages")
        if urls is None:
            # check for normalized name
            names = self.store.find_package(path)
            if names and names[0] != path:
                raise Redirect, self.config.simple_script + '/' + names[0]
            raise NotFound, path + " does not have any releases"
        html = []
        html.append("""<html><head><title>Links for %s</title></head>"""
                    % cgi.escape(path))
        html.append("<body><h1>Links for %s</h1>" % cgi.escape(path))
        for href, rel, text in urls:
            if href.startswith('http://cheeseshop.python.org/pypi') or \
                    href.startswith('http://pypi.python.org/pypi') or \
                    href.startswith('http://www.python.org/pypi'):
                # Suppress URLs that point to us
                continue
            if rel:
                rel = ' rel="%s"' % rel
            else:
                rel = ''
            href = cgi.escape(href, quote=True)
            text = cgi.escape(text)
            html.append("""<a href="%s"%s>%s</a><br/>\n""" % (href, rel, text))
        html.append("</body></html>")
        html = ''.join(html)
        return html

    def get_accept_encoding(self, supported):
        accept_encoding = self.env.get('HTTP_ACCEPT_ENCODING')
        if not accept_encoding:
            return None
        accept_encoding = accept_encoding.split(',')
        result = {}
        for s in accept_encoding:
            s = s.split(';') # gzip;q=0.6
            if len(s) == 1:
                result[s[0].strip()] = 1.0
            elif s[1].startswith('q='):
                result[s[0].strip()] = float(s[1][2:])
            else:
                # not the correct format
                result[s[0].strip()] = 1.0
        best_prio = 0
        best_enc = None
        for enc in supported:
            if enc in result:
                prio = result[enc]
            elif '*' in result:
                prio = result['*']
            else:
                prio = 0
            if prio > best_prio:
                best_prio, best_enc = prio, enc
        return best_enc

    def run_simple(self):
        path = self.env.get('PATH_INFO')
        if not path:
            raise Redirect, self.config.simple_script+'/'
        accept_encoding = self.get_accept_encoding(('identity', 'gzip'))
        if path=='/':
            html = []
            html.append("<html><head><title>Simple Index</title></head>")
            html.append("<body>\n")
            for name in self.store.get_packages_utf8():
                qname = urllib.quote(name)
                ename = cgi.escape(name)
                html.append("<a href='%s/'>%s</a><br/>\n" % (qname,ename))
            html.append("</body></html>")
            html = ''.join(html)
            self.handler.send_response(200, 'OK')
            if accept_encoding == 'gzip':
                stream = cStringIO.StringIO()
                # level 6 is what the gzip command line tool uses by default
                f = gzip.GzipFile(mode='wb', fileobj=stream, compresslevel=6)
                f.write(html)
                f.close()
                html = stream.getvalue()
                self.handler.send_header('Content-encoding', 'gzip')
            self.handler.set_content_type('text/html; charset=utf-8')
            self.handler.send_header('Content-Length', str(len(html)))
            self.handler.end_headers()
            self.wfile.write(html)
            return

        path = path[1:]
        if not path.endswith('/'):
            raise Redirect, self.config.simple_script + '/' + path + '/'
        path = path[:-1]
        if '/' not in path:
            html = self.simple_body(path)
            self.handler.send_response(200, 'OK')
            if accept_encoding == 'gzip':
                stream = cStringIO.StringIO()
                # level 6 is what the gzip command line tool uses by default
                f = gzip.GzipFile(mode='wb', fileobj=stream, compresslevel=6)
                f.write(html)
                f.close()
                html = stream.getvalue()
                self.handler.send_header('Content-encoding', 'gzip')
            self.handler.set_content_type('text/html; charset=utf-8')
            self.handler.send_header('Content-Length', str(len(html)))
            self.handler.end_headers()
            self.wfile.write(html)
            return
        raise NotFound, path

    def run_simple_sign(self):
        path = self.env.get('PATH_INFO')
        if not path.endswith('/'):
            raise Redirect, self.config.simple_sign_script+path+'/'
        path = path[1:-1]
        if '/' in path:
            raise NotFound, path
        html = self.simple_body(path)
        if not self.privkey:
            self.privkey = DSA.load_key(os.path.join(self.config.key_dir, 'privkey'))
        md = EVP.MessageDigest('sha1')
        md.update(html)
        digest = md.final()
        sig = self.privkey.sign_asn1(digest)
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('application/octet-stream')
        self.handler.end_headers()
        self.wfile.write(sig)

    def run_id(self):
        path = self.env.get('PATH_INFO')
        if not path:
            self.openid_discovery()
        else:
            self.openid_user(path)

    def home(self, nav_current='home'):
        self.write_template('home.pt', title='PyPI - the Python Package Index',
                            headers={'X-XRDS-Location':self.url_machine+'/id'})

    def about(self, nav_current='home'):
        self.write_template('about.pt', title='About PyPI')

    def rss(self):
        """Dump the last N days' updates as an RSS feed.
        """
        # determine whether the rss file is up to date
        if not os.path.exists(self.config.rss_file):
            self.rss_regen()

        # TODO: throw in a last-modified header too?
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/xml; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(open(self.config.rss_file).read())

    def packages_rss(self):
        """Dump the last N days' updates as an RSS feed.
        """
        # determine whether the rss file is up to date
        if not os.path.exists(self.config.packages_rss_file):
            self.rss_regen()

        # TODO: throw in a last-modified header too?
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/xml; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(open(self.config.packages_rss_file).read())

    def rss_regen(self):
        context = {}
        context['app'] = self

        # generate the releases RSS
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        template = PyPiPageTemplate('rss.xml', template_dir)
        content = template(**context)
        f = open(self.config.rss_file, 'w')
        try:
            f.write(content.encode('utf-8'))
        finally:
            f.close()

        # generate the packages RSS
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        template = PyPiPageTemplate('packages-rss.xml', template_dir)
        content = template(**context)
        f = open(self.config.packages_rss_file, 'w')
        try:
            f.write(content.encode('utf-8'))
        finally:
            f.close()

    def lasthour(self):
        self.write_template('rss1hour.xml', **{'content-type':'text/xml; charset=utf-8'})

    def browse(self, nav_current='browse'):
        ua = self.env.get('HTTP_USER_AGENT', '')
        if botre.search(ua) is not None:
            self.handler.send_response(200, 'OK')
            self.handler.set_content_type('text/plain')
            self.handler.end_headers()
            self.wfile.write('This page intentionally blank.')
            return

        self.nav_current = nav_current
        content = cStringIO.StringIO()
        w = content.write

        trove = self.store.trove()
        qs = cgi.parse_qsl(self.env.get('QUERY_STRING', ''))

        # Analyze query parameters c= and show=
        cat_ids = []
        show_all = False
        for x in qs:
            if x[0] == 'c':
                try:
                    c = int(x[1])
                except:
                    continue
                if trove.trove.has_key(c):
                    cat_ids.append(c)
            elif x[0] == 'show' and x[1] == 'all':
                show_all = True
        cat_ids.sort()

        # XXX with 18 classifiers, postgres runs out of memory
        # So limit the number of simultaneous classifiers
        if len(cat_ids) > 8:
            self.fail("Too many classifiers", code=500)
            return

        # Fetch data from the database
        if cat_ids:
            packages_, tally = self.store.browse(cat_ids)
        else:
            # use cached version of top-level browse page
            packages_, tally = self.store.browse_tally()

        # we don't need the database any more, so release it
        self.store.close()

        # group tally into parent nodes
        boxes = {}
        for id, count in tally:
            if id in cat_ids:
                # Don't provide link to a selected category
                continue
            node = trove.trove[id]
            parent = ' :: '.join(node.path_split[:-1])
            boxes.setdefault(parent, []).append((node.name, id, count))
        # Sort per box alphabetically by name
        for box in boxes.values():
            box.sort()

        # order the categories; some are hardcoded to be first: topic,
        # environment, framework
        available_categories_ = []
        for cat in ("Topic", "Environment", "Framework"):
            if boxes.has_key(cat):
                available_categories_.append((cat, boxes.pop(cat)))
        # Sort the rest alphabetically
        boxes = boxes.items()
        boxes.sort()
        available_categories_.extend(boxes)

        # ... build packages viewdata
        packages_count = len(packages_)
        packages = []
        for p in packages_:
            packages.append(dict(name=p[0], version=p[1], summary=p[2],
                url=self.packageURL(p[0], p[1])))

        # ... build selected categories viewdata
        selected_categories = []
        for c in cat_ids:
            n = trove.trove[c]
            unselect_url = "%s?:action=browse" % self.url_path
            for c2 in cat_ids:
                if c==c2: continue
                unselect_url += "&c=%d" % c2
            selected_categories.append(dict(path_string=cgi.escape(n.path),
                    path = n.path_split,
                    pathstr = path2str(n.path_split),
                    unselect_url = unselect_url))

        # ... build available categories viewdata
        available_categories = []
        for name, subcategories in available_categories_:
            sub = []
            for subcategory, fid, count in subcategories:
                if fid in cat_ids:
                    add = cat_ids
                else:
                    add = cat_ids + [fid]
                add.sort()
                url = self.url_path + '?:action=browse'
                for c in add:
                    url += "&c=%d" % c
                sub.append(dict(
                    name = subcategory,
                    packages_count = count,
                    url = url,
                    description = subcategory[-1]))

            available_categories.append(dict(
                subcategories=sub, name=name, id=id))

        # only show packages if they're less than 20 and the user has
        # selected some categories, or if the user has explicitly asked
        # for them all to be shown by passing show=all on the URL
        show_packages = selected_categories and \
            (packages_count < 30 or show_all)

        # render template
        url = self.url_path + '?:action=browse&show=all'
        for c in cat_ids:
            url += '&c=%d' % c
        self.write_template('browse.pt', title="Browse",
            show_packages_url=url,
            show_packages=show_packages, packages=packages,
            packages_count=packages_count,
            selected_categories=selected_categories,
            available_categories=available_categories,
            norobots=True)

    def logout(self):
        self.loggedin = False
        self.store.delete_cookie(self.usercookie)
        self.home()

    def clear_auth(self):
        if self.username:
            raise Unauthorised, "Clearing basic auth"
        self.home()

    def login(self):
        if 'provider' in self.form:
            # OpenID login
            for p in providers:
                if p[0] == self.form['provider']:
                    break
            else:
                return self.fail('Unknown provider')
            stypes, url, assoc_handle = self.store.get_provider_session(p)
            return_to = self.config.url+'?:action=openid_return'
            url = openid2rp.request_authentication(stypes, url, assoc_handle, return_to)
            self.store.commit()
            raise RedirectTemporary(url)
        if 'openid_identifier' in self.form:
            # OpenID with explicit ID
            kind, claimed_id = openid2rp.normalize_uri(self.form['openid_identifier'])
            if kind == 'xri':
                res = openid2rp.resolve_xri(claimed_id)
                if res:
                    # A.5: XRI resolution requires to use canonical ID
                    # Original claimed ID may be preserved for display
                    # purposes
                    claimed_id = res[0]
                    res = res[1:]
            else:
                res = openid2rp.discover(claimed_id)
            if not res:
                return self.fail('Discovery failed. If you think this is in error, please submit a bug report.')
            stypes, op_endpoint, op_local = res
            self.store.store_discovered(claimed_id, stypes, op_endpoint, op_local)
            if not op_local:
                op_local = claimed_id
            try:
                assoc_handle = self.store.get_session_for_endpoint(op_endpoint, stypes)
            except ValueError, e:
                return self.fail('Cannot establish OpenID session: ' + str(e))
            return_to = self.config.url+'?:action=openid_return'
            url = openid2rp.request_authentication(stypes, op_endpoint, assoc_handle, return_to, claimed_id, op_local)
            self.store.commit()
            raise RedirectTemporary(url)
        if not self.authenticated:
            raise Unauthorised
        self.usercookie = self.store.create_cookie(self.username)
        self.store.get_token(self.username)
        self.loggedin = 1
        self.home()

    def role_form(self):
        ''' A form used to maintain user Roles
        '''
        self.nav_current = 'role_form'
        package_name = ''
        if self.form.has_key('package_name'):
            package_name = self.form['package_name']
            if not (self.store.has_role('Admin', package_name) or
                    self.store.has_role('Owner', package_name)):
                raise Unauthorised
            package = '''
<tr><th>Package Name:</th>
    <td><input type="text" readonly name="package_name" value="%s"></td>
</tr>
'''%cgi.escape(package_name)
        elif not self.store.has_role('Admin', ''):
            raise Unauthorised
        else:
            names = [x['name'] for x in self.store.get_packages()]
            names.sort()
            names = map(cgi.escape, names)
            s = '\n'.join(['<option value="%s">%s</option>'%(name, name)
                            for name in names])
            package = '''
<tr><th>Package Name:</th>
    <td><select name="package_name">%s</select></td>
</tr>
'''%s

        self.write_template('role_form.pt', title='Role maintenance',
            name=package_name, package=package)

    def package_role_list(self, name, heading='Assigned Roles'):
        ''' Generate an HTML fragment for a package Role display.
        '''
        l = ['<table class="roles">',
             '<tr><th class="header" colspan="2">%s</th></tr>'%heading,
             '<tr><th>User</th><th>Role</th></tr>']
        for assignment in self.store.get_package_roles(name):
            username = assignment[1]
            user = self.store.get_user(username)
            keyid = user['gpg_keyid']
            if keyid:
                username = "%s (PGP key %s)" % (username, keyid)
            l.append('<tr><td>%s</td><td>%s</td></tr>'%(
                cgi.escape(username),
                cgi.escape(assignment[0])))
        l.append('</table>')
        return '\n'.join(l)

    def role(self):
        ''' Add a Role to a user.
        '''
        # required fields
        if not self.form.has_key('package_name'):
            raise FormError, 'package_name is required'
        if not self.form.has_key('user_name'):
            raise FormError, 'user_name is required'
        if not self.form.has_key('role_name'):
            raise FormError, 'role_name is required'
        if not self.form.has_key(':operation'):
            raise FormError, ':operation is required'

        # get the values
        package_name = self.form['package_name']
        user_name = self.form['user_name']
        role_name = self.form['role_name']

        # make sure only admins and owners can add roles
        if not (self.store.has_role('Admin', package_name) or
                self.store.has_role('Owner', package_name)):
            raise Unauthorised

        self.csrf_check()

        # further vali:dation
        if role_name not in ('Owner', 'Maintainer'):
            raise FormError, 'role_name not Owner or Maintainer'
        if not self.store.has_user(user_name):
            raise FormError, "user doesn't exist"

        # add or remove
        operation = self.form[':operation']
        if operation == 'Add Role':
            # make sure the user doesn't have the role
            if self.store.has_role(role_name, package_name, user_name):
                raise FormError, 'user has that role already'
            self.store.add_role(user_name, role_name, package_name)
            self.store.changed()
            self.ok_message = 'Role Added OK'
        else:
            # make sure the user has the role
            if not self.store.has_role(role_name, package_name, user_name):
                raise FormError, "user doesn't have that role"
            if user_name == self.username:
                # Check that we are not removing the last maintainer
                for role in self.store.get_package_roles(package_name):
                    if role['role_name']=='Owner' and role['user_name'] != user_name:
                        break
                else:
                    # No other owner found
                    raise FormError, "You can't remove yourself as the last owner; remove the package instead"
            self.store.delete_role(user_name, role_name, package_name)
            self.store.changed()
            self.ok_message = 'Role Removed OK'
            if user_name == self.username:
                # Might not have access to the package anymore
                return self.home()

        self.role_form()

    def _get_latest_pkg_info(self, name, version):
        # get the appropriate package info from the database
        if name is None:
            try:
                name = self.form['name']
            except KeyError:
                raise NotFound, 'no package name supplied'
        if version is None:
            if self.form.get('version'):
                version = self.form['version']
            else:
                l = self.store.get_latest_release(name, hidden=False)
                try:
                    version = l[0][1]
                except IndexError:
                    raise NotFound, 'no releases'
        info = self.store.get_package(name, version)
        if not info:
            raise NotFound, 'invalid name/version'
        return info, name, version

    def doap(self, name=None, version=None):
        '''Return DOAP rendering of a package.
        '''
        info, name, version = self._get_latest_pkg_info(name, version)

        root = cElementTree.Element('rdf:RDF', {
            'xmlns:rdf': "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            'xmlns:foaf': "http://xmlns.com/foaf/0.1/",
            'xmlns': "http://usefulinc.com/ns/doap#"})
        SE = cElementTree.SubElement

        project = SE(root, 'Project')

        def write_element(parent, attr, element):
            value = info[attr]
            if not value or value == 'UNKNOWN':
                return
            element = SE(parent, element)
            element.text = value
            element.tail = '\n'

         # Not handled here: version, keywords
        for attr, element in [('name', 'name'),
                              ('summary', 'shortdesc'),
                              ('description', 'description'),
                              ('download_url', 'download-page')
                              ]:
              write_element(project, attr, element)

        url = info['home_page']
        if url and url != 'UNKNOWN':
            url = SE(project, 'homepage', {'rdf:resource': url})
            url.tail = '\n'

        person = 'maintainer'
        if not info[person]:
            person = 'author'
        if info[person]:
            maint = SE(project, 'maintainer')
            pelem = SE(maint, 'foaf:Person')
            write_element(pelem, person, 'foaf:name')
            email = info[person+'_email']
            if email and email != 'UNKNOWN':
                # sha1 requires ascii not unicode
                if isinstance(email, unicode): email = email.encode('utf8')
                obj = hashlib.sha1(email)
                email = binascii.b2a_hex(obj.digest())
                elem = SE(pelem, 'foaf:mbox_sha1sum')
                elem.text = email
            maint.tail = '\n'

        # Write version
        version = info['version']
        if version:
            release = SE(project, 'release')
            release.tail = '\n'
            velem = SE(release, 'Version')
            revision = SE(velem, 'revision')
            revision.text = version

        # write XML
        s = cStringIO.StringIO()
        s.write('<?xml version="1.0" encoding="UTF-8" ?>\n')
        cElementTree.ElementTree(root).write(s, 'utf-8')
        filename = '%s-%s.xml'%(name.encode('ascii', 'replace'),
            version.encode('ascii', 'replace'))

        self.handler.send_response(200, "OK")
        self.handler.set_content_type('text/xml; charset="UTF-8"')
        self.handler.send_header('Content-Disposition',
            'attachment; filename=%s'%filename)
        self.handler.end_headers()
        self.wfile.write(s.getvalue())

    def json(self, name=None, version=None):
        '''Return JSON rendering of a package.
        '''
        info, name, version = self._get_latest_pkg_info(name, version)
        d = {
            'info': rpc.release_data(self.store, name, version),
            'urls': rpc.release_urls(self.store, name, version),
        }
        for url in d['urls']:
            url['upload_time'] = url['upload_time'].strftime('%Y-%m-%dT%H:%M:%S')
        self.handler.send_response(200, "OK")
        self.handler.set_content_type('application/json; charset="UTF-8"')
        #filename = '%s-%s.json'%(name.encode('ascii', 'replace'),
        #    version.encode('ascii', 'replace'))
        #self.handler.send_header('Content-Disposition',
        #    'attachment; filename=%s'%filename)
        self.handler.send_header('Content-Disposition', 'inline')
        self.handler.end_headers()
        # write the JSONP extra crap if necessary
        s = json.dumps(d, indent=4)
        callback = self.form.get('callback')
        if callback:
            s = '%s(%s)' % (callback, s)
        self.wfile.write(s)

    def display_pkginfo(self, name=None, version=None):
        '''Reconstruct and send a PKG-INFO metadata file.
        '''
        # XXX tarek need to add 1.2 support here
        #
        info, name, version = self._get_latest_pkg_info(name, version)
        if not info:
            return self.fail('No such package / version',
                heading='%s %s'%(name, version),
                content="I can't find the package / version you're requesting")

        content = cStringIO.StringIO()
        def w(s):
            if isinstance(s, unicode): s = s.encode('utf8')
            content.write(s)

        # We're up to PEP 314
        w("Metadata-Version: 1.1\n")

        # now the package info
        keys = info.keys()
        keys.sort()
        keypref = 'name version author author_email maintainer maintainer_email home_page download_url summary license description keywords platform'.split()
        for key in keypref:
            value = info[key]
            if not value:
                continue

            label = key.capitalize().replace('_', '-')

            if key == 'description':
                value = rfc822_escape(value)
            elif key.endswith('_email'):
                value = cgi.escape(value)
                value = value.replace('@', ' at ')
                value = value.replace('.', ' ')
            w('%s: %s\n'%(label, value))

        for col in ('requires', 'provides', 'obsoletes'):
            l = self.store.get_release_relationships(name, version, col)
            for entry in l:
                w('%s: %s\n' %(col.capitalize(), entry['specifier']))

        classifiers = self.store.get_release_classifiers(name, version)
        for c in classifiers:
            w('Classifier: %s\n' % (c['classifier'],))
        w('\n')

        # Not using self.success or page_head because we want
        # plain-text without all the html trappings.
        self.handler.send_response(200, "OK")
        self.handler.set_content_type('text/plain; charset=utf-8')
        self.handler.end_headers()
        s = content.getvalue()
        self.wfile.write(s)

    def release_nav(self):
        name = self.form.get('name')
        if not name:
            return ''

        # permission to do this?
        if not self.loggedin:
            return
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            return ''

        # determine the version
        version = self.form.get('version')
        if not version:
            l = self.store.get_latest_release(name, hidden=False)
            try:
                version = l[-1][1]
            except IndexError:
                version = "(latest release)"

        un = urllib.quote_plus(name.encode('utf-8'))
        uv = urllib.quote_plus(version.encode('utf-8'))
        url = '%s?name=%s&amp;version=%s'%(self.url_path, un, uv)
        return '''<p class="release-nav">Package:
  <a href="%s?:action=role_form&amp;package_name=%s">admin</a> |
  <a href="%s?:action=pkg_edit&amp;name=%s">releases</a> |
  <a href="%s&amp;:action=display">view</a> |
  <a href="%s&amp;:action=submit_form">edit</a> |
  <a href="%s&amp;:action=files">files</a> |
  <a href="%s&amp;:action=display_pkginfo">PKG-INFO</a>
</p>'''%(self.url_path, un, self.url_path, un, url, url, url, url)

    def quote_plus(self, data):
        return urllib.quote_plus(data)

    def _load_release_info(self, name, version):
        '''Determine the information about a release of the named package.

        If version is specified then we return that version and also determine
        what the latest version of the package is.

        If the version is None then we return the latest version.
        '''
        # get the appropriate package info from the database
        if name is None:
            # check that we get one - and only one - name argument
            name = self.form.get('name', None)
            if name is None or isinstance(name, list):
                self.fail("Which package do you want to display?")

        using_latest = False
        if version is None:
            if self.form.get('version'):
                version = self.form['version']
            else:
                l = self.store.get_package_releases(name, hidden=False)
                if len(l) > 1:
                    raise MultipleReleases(releases=l)
                l = self.store.get_latest_release(name, hidden=False)
                try:
                    version = l[-1][1]
                except IndexError:
                    using_latest = True
                    version = "(latest release)"

        if not using_latest:
             l = self.store.get_package_releases(name, hidden=False)
             latest_version = self.store.get_latest_release(name, hidden=False)
             try:
                 latest_version = l[0][1]
             except:
                 latest_version = 'Unknown'
                 # for now silently fail, this simply means we were not able
                 # to determine what the latest release is for one reason or
                 # another
        else:
             latest_version = None

        info = self.store.get_package(name, version)
        if not info:
            raise NotFound
        return info, latest_version

    def display(self, name=None, version=None, ok_message=None,
            error_message=None):
        ''' Print up an entry
        '''
        try:
            info, latest_version = self._load_release_info(name, version)
        except MultipleReleases, e:
            return self.index(releases=e.releases)

        name = info['name']
        version = info['version']
        using_latest = latest_version==version

# RJ: disabled cheesecake because the (strange) errors were getting annoying
#        columns = 'name version author author_email maintainer maintainer_email home_page download_url summary license description description_html keywords platform cheesecake_installability_id cheesecake_documentation_id cheesecake_code_kwalitee_id'.split()
        columns = ('name version author author_email maintainer '
                   'maintainer_email home_page requires_python download_url '
                   'summary license description description_html keywords '
                   'platform bugtrack_url').split()

        release = {'description_html': ''}
        bugtrack_url =''
        for column in columns:
            value = info[column]
            if not info[column]: continue
            if isinstance(value, basestring) and value.strip() in (
                    'UNKNOWN', '<p>UNKNOWN</p>'): continue
            if column in ('name', 'version'): continue
            if column == 'description':
                # fallback if no description_html
                release['description_html'] = '<p>%s</p>'%newline_to_br(
                    cgi.escape(value))
            elif column.endswith('_email'):
                column = column[:column.find('_')]
                if release.has_key(column):
                    start = release[column]
                else:
                    start = ''
                value = value.replace('@', ' at ')
                value = value.replace('.', ' ')
                value = '%s <%s>'%(start, value)
            elif column.startswith('cheesecake_'):
                column = column[:-3]
                value = self.store.get_cheesecake_index(int(value))
            elif column == 'bugtrack_url':
                bugtrack_url = value
            value = info[column]
            release[column] = value

        roles = {}
        for role, user in self.store.get_package_roles(name):
            roles.setdefault(role, []).append(user)

        def values(col):
            l = self.store.get_release_relationships(name, version, col)
            return [x['specifier'] for x in l]

        categories = []
        is_py3k = False
        for c in self.store.get_release_classifiers(name, version):
            path = str2path(c['classifier'])
            pathstr = path2str(path)
            if pathstr.startswith('Programming Language :: Python :: 3'):
                is_py3k = True
            url = "%s?:action=browse&c=%s" % (self.url_path, c['trove_id'])
            categories.append(dict(
                name = c['classifier'],
                path = path,
                pathstr = pathstr,
                url = url,
                id = c['trove_id']))

        latest_version_url = '%s/%s/%s' % (self.config.url, name,
                                           latest_version)

        # New metadata
        requires_dist = self.store.get_package_requires_dist(name, version)
        provides_dist = self.store.get_package_provides_dist(name, version)
        obsoletes_dist = self.store.get_package_obsoletes_dist(name, version)
        project_url = self.store.get_package_project_url(name, version)
        requires_external = self.store.get_package_requires_external(name, version)

        docs = self.store.docs_url(name)
        files = self.store.list_files(name, version)

        self.write_template('display.pt',
                            name=name, version=version, release=release,
                            description=release.get('summary') or name,
                            keywords=release.get('keywords', ''),
                            title=name + " " +version,
                            requires=values('requires'),
                            provides=values('provides'),
                            obsoletes=values('obsoletes'),
                            files=files,
                            docs=docs,
                            categories=categories,
                            is_py3k=is_py3k,
                            roles=roles,
                            newline_to_br=newline_to_br,
                            usinglatest=using_latest,
                            latestversion=latest_version,
                            latestversionurl=latest_version_url,
                            action=self.link_action(),
                            requires_dist=requires_dist,
                            provides_dist=provides_dist,
                            obsoletes_dist=obsoletes_dist,
                            requires_external=requires_external,
                            project_url=project_url,
			    bugtrack_url = bugtrack_url,
                            requires_python=release.get('requires_python', ''))

    def index(self, nav_current='index', releases=None):
        ''' Print up an index page
        '''
        self.nav_current = nav_current
        if releases is None:
            spec = self.form_metadata()
            if not spec.has_key('_pypi_hidden'):
                spec['_pypi_hidden'] = False
            i=0
            l = self.store.query_packages(spec)
            if len(l) == 1:
                self.form['name'] = l[0]['name']
                self.form['version'] = l[0]['version']
                return self.display()
        else:
            l = releases
        self.write_template('index.pt', title="Index of Packages",
            matches=l)

    STOPWORDS = set([
        "a", "and", "are", "as", "at", "be", "but", "by",
        "for", "if", "in", "into", "is", "it",
        "no", "not", "of", "on", "or", "such",
        "that", "the", "their", "then", "there", "these",
        "they", "this", "to", "was", "will",
    ])
    def search(self, nav_current='index'):
        ''' Search for the indicated term.

        Try name first, then summary then description. Collate a
        score for each package that matches.
        '''
        term = self.form.get('term', '').strip().lower()
        term = re.sub(r'[^\w\s\.\-]', '', term)
        terms = [t for t in term.split() if t not in self.STOPWORDS]
        terms = filter(None, terms)
        if not terms:
            raise FormError, 'You need to supply a search term'

        d = {}
        columns = [
            ('name', 4),      # doubled for exact (case-insensitive) match
            ('summary', 2),
            ('keywords', 2),
            ('description', 1),
            ('author', 1),
            ('maintainer', 1),
        ]


        # score all package/release versions
        # require that each term occurs at least once (AND search)
        for t in terms:
            d_new = {}
            for col, score in columns:
                spec = {'_pypi_hidden': False, col: t}
                for r in self.store.query_packages(spec):
                    key = (r['name'], r['version'])
                    if d:
                        # must find current score in d
                        if key not in d:
                            # not a candidate anymore
                            continue
                        else:
                            e = d[key]
                    else:
                        # may find score in d_new
                        e = d_new.get(key, [0, r])
                    if col == 'name' and t == r['name'].lower():
                        e[0] += score*2
                    else:
                        e[0] += score
                    d_new[key] = e
            d = d_new
            if not d:
                # no packages match
                break

        # record the max value of _pypi_ordering per package
        max_ordering = {}
        for score,r in d.values():
            old_max = max_ordering.get(r['name'], -1)
            max_ordering[r['name']] = max(old_max, r['_pypi_ordering'])

        # drop old releases
        for (name, version), (score, r) in d.items():
            if max_ordering[name] != r['_pypi_ordering']:
                del d[(name, version)]

        # now sort by score and name ordering
        l = []
        scores = {}
        for k,v in d.items():
            l.append((-v[0], k[0].lower(), v[1]))
            scores[k[0]] = v[0]

        if len(l) == 1:
            raise RedirectTemporary, "%s/%s/%s" % (self.config.url,l[0][-1]['name'],l[0][-1]['version'])

        # sort and pull out just the record
        l.sort()
        l = [e[-1] for e in l]

        self.write_template('index.pt', matches=l, scores=scores,
            title="Index of Packages Matching '%s'"%term)

    def submit_form(self):
        ''' A form used to submit or edit package metadata.
        '''
        # submission of this form requires a login, so we should check
        # before someone fills it in ;)
        if not self.authenticated:
            raise Unauthorised, 'You must log in.'

        if not self.loggedin:
            # Authenticated, but not logged in - auto-login
            self.loggedin = True
            self.usercookie = self.store.create_cookie(self.username)
            token = self.store.get_token(self.username)

        # are we editing a specific entry?
        info = {}
        name = version = None
        if self.form.has_key('name') and self.form.has_key('version'):
            name = self.form['name']
            version = self.form['version']

            # permission to do this?
            if not (self.store.has_role('Owner', name) or
                    self.store.has_role('Admin', name) or
                    self.store.has_role('Maintainer', name)):
                raise Forbidden, 'Not Owner or Maintainer'

            # get the stored info
            for k,v in self.store.get_package(name, version).items():
                info[k] = v

        self.nav_current = 'submit_form'

        content= cStringIO.StringIO()
        w = content.write

        # display all the properties
        for property in 'name version author author_email maintainer maintainer_email home_page license summary description keywords platform download_url _pypi_hidden bugtrack_url'.split():
            # get the existing entry
            if self.form.has_key(property):
                value = self.form[property]
            else:
                value = info.get(property, '')
            if value is None:
                value = ''

            # form field
            if property in ('name', 'version'):
                # not editable
                field = '<input id="%s" type="hidden" name="%s" value="%s">'%(
                    "property_%s" % property, property, urllib.quote(value.encode('utf-8')))
            if property == '_pypi_hidden':
                a = b = ''
                if value:
                    b = ' selected'
                else:
                    a = ' selected'
                field = '''<select name="_pypi_hidden">
                            <option value="0"%s>No</option>
                            <option value="1"%s>Yes</option>
                           </select>'''%(a,b)
            elif property in ('license', 'platform'):
                field = '''<textarea name="%s" rows="5"
                    cols="80">%s</textarea><br />You should enter a full
                    description here only if appropriate classifiers aren\'t
                    available (see below).'''%(property, cgi.escape(value))
            elif property.endswith('description'):
                field = '''<textarea name="%s" rows="5"
                    cols="80">%s</textarea><br />You may use
                    <a target="_new" href="http://docutils.sf.net/rst.html">ReStructuredText</a>
                    formatting for this field.'''%(property,
                    cgi.escape(value))
            else:
                field = '<input id="%s" size="60" name="%s" value="%s">'%(
                    "property_%s" % property, property, cgi.escape(value))

            # now spit out the form line
            label = property.replace('_', '&nbsp;').capitalize()
            if label in ('Name', 'Version'):
                req = 'class="required"'
            elif property == 'download_url':
                label = "Download URL"
            elif property == '_pypi_hidden':
                label = "Hidden"
            elif property == 'CSRFToken':
                field = '<input type="hidden" name="CSRFToken" value="%s">' % (
                        token,)
            else:
                req = ''
            w('<tr><th %s>%s:</th><td>%s</td></tr>\n'%(req, label,
                field.encode('utf8')))

        # format relationships
        def relationship_input(relationship, value):
            w('''<tr><th>%s:</th>
   <td><textarea name="%s" cols="40" rows="5">%s</textarea></td>'''%(
    relationship.capitalize(), relationship,
    '\n'.join([v['specifier'] for v in value])))
        for col in ('requires', 'provides', 'obsoletes'):
            if name is not None:
                l = self.store.get_release_relationships(name, version, col)
            else:
                l = []
            relationship_input(col, l)

        # if we're editing
        if name is not None:
            release_cifiers = {}
            for classifier in self.store.get_release_classifiers(name, version):
                release_cifiers[classifier['classifier']] = 1
        else:
            release_cifiers = {}

        # now list 'em all
        w('''<tr><th>Classifiers:</th>
  <td><select multiple name="classifiers" size="10">
''')
        for classifier in self.store.get_classifiers():
            ctext = classifier['classifier']
            selected = release_cifiers.has_key(ctext) and ' selected' or ''
            htext = cgi.escape(ctext)
            w('<option%s value="%s">%s</option>'%(selected, htext, htext))

        w('''</select></td></tr>''')

        self.write_template('submit_form.pt',
            title='Submitting package information',
            fields=content.getvalue().decode('utf8'))

    def csrf_check(self):
        '''Check that the required CSRF token is present in the form
        submission.
        '''
        if self.form.get('CSRFToken') != self.store.get_token(self.username):
            raise FormError, "Form Failure; reset form submission"

    def submit_pkg_info(self):
        ''' Handle the submission of distro metadata as a PKG-INFO file.
        '''
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to store package information"

        if not self.form.has_key('pkginfo'):
            raise FormError, \
                "You must supply the PKG-INFO file"

        self.csrf_check()

        # get the data
        pkginfo = self.form['pkginfo']
        if isinstance(pkginfo, FileUpload):
            pkginfo = pkginfo.value
        elif isinstance(pkginfo, str):
            try:
                pkginfo = pkginfo.decode('utf8')
            except UnicodeDecodeError:
                raise FormError, \
                    "Your PKG-INFO file must be either ASCII or UTF-8. " \
                    "If this is inconvenient, use 'python setup.py register'."
        mess = email.message_from_file(cStringIO.StringIO(pkginfo))
        data = {}
        for k, v in mess.items():
            # clean up the keys and values, normalise "-" to "_"
            k = k.lower().replace('-', '_')
            v = v.strip()

            # Platform, Classifiers, ...?
            if data.has_key(k):
                l = data[k]
                if isinstance(l, types.ListType):
                    l.append(v)
                else:
                    data[k] = [l, v]
            else:
                data[k] = v

        # flatten platforms into one string
        platform = data.get('platform', '')
        if isinstance(platform, types.ListType):
            data['platform'] = ','.join(data['platform'])

        # make sure relationships are lists
        for name in ('requires', 'provides', 'obsoletes',
                     'requires_dist', 'provides_dist',
                     'obsoletes_dist',
                     'requires_external', 'project_url'):
            if data.has_key(name) and not isinstance(data[name],
                    types.ListType):
                data[name] = [data[name]]

        # rename classifiers
        if data.has_key('classifier'):
            classifiers = data['classifier']
            if not isinstance(classifiers, types.ListType):
                classifiers = [classifiers]
            data['classifiers'] = classifiers

        # validate the data
        try:
            self.validate_metadata(data)
        except ValueError, message:
            raise FormError, message

        name = data['name']
        version = data['version']

        # don't hide by default
        if not data.has_key('_pypi_hidden'):
            data['_pypi_hidden'] = '0'

        # make sure the user has permission to do stuff
        has_package = self.store.has_package(name)
        if has_package and not (
                self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            raise Forbidden, \
                "You are not allowed to store '%s' package information"%name

        if not has_package:
            names = self.store.find_package(name)
            if names:
                raise Forbidden, "Package name conflicts with existing package '%s'" % names[0]

        # save off the data
        message = self.store.store_package(name, version, data)
        self.store.changed()

        # return a display of the package
        self.form['name'] = data['name']
        self.form['version'] = data['version']
        self.display(ok_message=message)

    def submit(self, parameters=None, response=True):
        ''' Handle the submission of distro metadata.
        '''
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to store package information"

        if parameters is None:
            parameters = self.form

        # pull the package information out of the form submission
        data = self.form_metadata(parameters)

        # validate the data
        try:
            self.validate_metadata(data)
        except ValueError, message:
            raise FormError, message

        name = data['name']
        version = data['version']

        # make sure the user has permission to do stuff
        has_package = self.store.has_package(name)
        if has_package and not (
                self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            raise Forbidden, \
                "You are not allowed to store '%s' package information"%name

        if not has_package:
            names = self.store.find_package(name)
            if names:
                raise Forbidden, "Package name conflicts with existing package '%s'" % names[0]

        # make sure the _pypi_hidden flag is set
        if not data.has_key('_pypi_hidden'):
            data['_pypi_hidden'] = False

        # save off the data
        message = self.store.store_package(name, version, data)
        self.store.changed()

        if response:
            # return a display of the package
            self.display(ok_message=message)

    def form_metadata(self, submitted_data=None):
        ''' Extract metadata from the form.
        '''
        if submitted_data is None:
            submitted_data = self.form
        data = {}
        for k in submitted_data:
            if k.startswith(':'): continue
            v = self.form[k]
            if k == '_pypi_hidden':
                v = v == '1'
            elif k in ('requires', 'provides', 'obsoletes',
                       'requires_dist', 'provides_dist',
                       'obsoletes_dist',
                       'requires_external', 'project_url'):
                if not isinstance(v, list):
                    v = [x.strip() for x in re.split('\s*[\r\n]\s*', v)]
                else:
                    v = [x.strip() for x in v]
                v = filter(None, v)
            elif isinstance(v, list):
                if k == 'classifiers':
                    v = [x.strip() for x in v]
                else:
                    v = ','.join([x.strip() for x in v])
            elif isinstance(v, FileUpload):
                continue
            else:
                v = v.strip()
            data[k.lower()] = v

        # make sure relationships are lists
        for name in ('requires', 'provides', 'obsoletes',
                     'requires_dist', 'provides_dist',
                     'obsoletes_dist',
                     'requires_external', 'project_url'):
            if data.has_key(name) and not isinstance(data[name],
                    types.ListType):
                data[name] = [data[name]]

        # make sure classifiers is a list
        if data.has_key('classifiers'):
            classifiers = data['classifiers']
            if not isinstance(classifiers, types.ListType):
                classifiers = [classifiers]
            data['classifiers'] = classifiers

        return data

    def verify(self):
        ''' Validate the input data.
        '''
        data = self.form_metadata()
        try:
            self.validate_metadata(data)
        except ValueError, message:
            self.fail(str(message), code=400, heading='Package verification')
            return

        self.write_template('message.pt', title='Package verification',
            message='Validated OK')

    def _validate_metadata_1_2(self, data):
        # loading the metadata into
        # a DistributionMetadata instance
        # so we can use its check() method
        metadata = Metadata()
        for key, value in data.items():
            metadata[key] = value
        metadata['Metadata-Version'] = '1.2'
        missing, warnings = metadata.check()

        # raising the first problem
        if len(missing) > 0:
            raise ValueError, '"%s" is missing' % missing[0]

        if len(warnings) > 0:
            raise ValueError, warnings[0]

    def validate_metadata(self, data):
        ''' Validate the contents of the metadata.
        '''
        if not data.get('name', ''):
            raise ValueError, 'Missing required field "name"'
        if not data.get('version', ''):
            raise ValueError, 'Missing required field "version"'
        if data.has_key('metadata_version'):
            metadata_version = data['metadata_version']
            del data['metadata_version']
        else:
            metadata_version = '1.0'  # default

        # Traditionally, package names are restricted only for
        # technical reasons; / is not allowed because it may be
        # possible to break path names for file and documentation
        # uploads
        if '/' in data['name']:
            raise ValueError, "Invalid package name"

        # check requires and obsoletes
        def validate_version_predicates(col, sequence):
            try:
                map(versionpredicate.VersionPredicate, sequence)
            except ValueError, message:
                raise ValueError, 'Bad "%s" syntax: %s'%(col, message)
        for col in ('requires', 'obsoletes'):
            if data.has_key(col) and data[col]:
                validate_version_predicates(col, data[col])

        # check provides
        if data.has_key('provides') and data['provides']:
            try:
                map(versionpredicate.check_provision, data['provides'])
            except ValueError, message:
                raise ValueError, 'Bad "provides" syntax: %s'%message

        # check PEP 345 fields
        if metadata_version == '1.2':
            self._validate_metadata_1_2(data)

        # check classifiers
        if data.has_key('classifiers'):
            d = {}
            for entry in self.store.get_classifiers():
                d[entry['classifier']] = 1
            for entry in data['classifiers']:
                if d.has_key(entry):
                    continue
                raise ValueError, 'Invalid classifier "%s"'%entry

    def pkg_edit(self):
        ''' Edit info about a bunch of packages at one go
        '''
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to edit package information"

        # this is used to render the form as well as edit it... UGH
        #self.csrf_check()

        name = self.form['name']

        if self.form.has_key('submit_remove'):
            return self.remove_pkg()

        # make sure the user has permission to do stuff
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            raise Forbidden, \
                "You are not allowed to edit '%s' package information"%name

        if self.form.has_key('submit_autohide'):
            value = self.form.has_key('autohide')
            self.store.set_package_autohide(name, value)

        # look up the current info about the releases
        releases = list(self.store.get_package_releases(name))
        reldict = {}
        for release in releases:
            info = {}
            for k,v in release.items():
                info[k] = v
            reldict[info['version']] = info

        # see if we're editing (note that form keys don't get unquoted)
        for key in self.form.keys():
            if key.startswith('hid_'):
                ver = urllib.unquote(key[4:])
                info = reldict[ver]
                info['_pypi_hidden'] = self.form[key] == '1'
            elif key.startswith('sum_'):
                ver = urllib.unquote(key[4:])
                info = reldict[ver]
                info['summary'] = self.form[key]

        # update the database
        for version, info in reldict.items():
            self.store.store_package(name, version, info)
            self.store.changed()

        self.write_template('pkg_edit.pt', releases=releases, name=name,
                            autohide=self.store.get_package_autohide(name),
            title="Package '%s' Editing"%name)

    def remove_pkg(self):
        ''' Remove a release or a whole package from the db.

            Only owner may remove an entire package - Maintainers may
            remove releases.
        '''
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to edit package information"

        self.csrf_check()

        # vars
        name = self.form['name']
        cn = cgi.escape(name)
        if self.form.has_key('version'):
            if isinstance(self.form['version'], type([])):
                version = [x for x in self.form['version']]
            else:
                version = [self.form['version']]
            cv = cgi.escape(', '.join(version))
            s = len(version)>1 and 's' or ''
            desc = 'release%s %s of package %s.'%(s, cv, cn)
        else:
            version = []
            desc = 'all information about package %s.'%cn

        # make sure the user has permission to do stuff
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                (version and self.store.has_role('Maintainer', name))):
            raise Forbidden, \
                "You are not allowed to edit '%s' package information"%name

        if self.form.has_key('submit_ok'):
            # ok, do it
            if version:
                for v in version:
                    self.store.remove_release(name, v)
                self.store.changed()
                self.ok_message='Release removed'
            else:
                self.store.remove_package(name)
                self.store.changed()
                self.ok_message='Package removed'
                return self.home()

        elif self.form.has_key('submit_cancel'):
            self.ok_message='Removal cancelled'

        else:
            message = '''You are about to remove %s<br />
                This action <em>cannot be undone</em>!<br />
                Are you <strong>sure</strong>?'''%desc

            fields = [
                {'name': ':action', 'value': 'remove_pkg'},
                {'name': 'name', 'value': name},
            ]
            for v in version:
                fields.append({'name': 'version', 'value': v})

            return self.write_template('dialog.pt', message=message,
                title='Confirm removal of %s'%desc, fields=fields)

        self.pkg_edit()

    # alias useful for the files ZPT page
    dist_file_types = store.dist_file_types
    dist_file_types_d = store.dist_file_types_d
    def files(self):
        '''List files and handle file submissions.
        '''
        name = version = None
        if self.form.has_key('name'):
            name = self.form['name']
        if self.form.has_key('version'):
            version = self.form['version']
        if not name or not version:
            self.fail(heading='Name and version are required',
                message='Name and version are required')
            return

        # if allowed, handle file upload
        maintainer = False
        if self.store.has_role('Maintainer', name) or \
                self.store.has_role('Admin', name) or \
                self.store.has_role('Owner', name):
            maintainer = True
            if self.form.has_key('submit_upload'):
                self.file_upload(response=False)

            elif (self.form.has_key('submit_remove') and
                    self.form.has_key('file-ids')):

                fids = self.form['file-ids']
                if isinstance(fids, list):
                    fids = [v for v in fids]
                else:
                    fids = [fids]

                for digest in fids:
                    try:
                        self.store.remove_file(digest)
                    except KeyError:
                        return self.fail('No such files to remove', code=200)
                    else:
                        self.store.changed()

        self.write_template('files.pt', name=name, version=version,
            maintainer=maintainer, title="Files for %s %s"%(name, version))

    def pretty_size(self, size):
        n = 0
        while size > 1024:
            size /= 1024
            n += 1
        return '%d%sB'%(size, ['', 'K', 'M', 'G'][n])

    def show_md5(self):
        if not self.form.has_key('digest'):
            return NotFound
            #raise ValueError, 'invalid MD5 digest'
        digest = self.form['digest']
        try:
            self.store.get_file_info(digest)
        except KeyError:
            # invalid MD5 digest - it's not in the database
            raise NotFound
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(digest)

    CURRENT_UPLOAD_PROTOCOL = "1"
    def file_upload(self, response=True, parameters=None):
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to edit package information"

        if parameters is None:
            parameters = self.form

        # can't perform CSRF check as this is invoked by tools
        #self.csrf_check()

        # Verify protocol version
        if parameters.has_key('protocol_version'):
            protocol_version = parameters['protocol_version']
        else:
            protocol_version = self.CURRENT_UPLOAD_PROTOCOL

        if protocol_version!=self.CURRENT_UPLOAD_PROTOCOL:
            # If a new protocol version is added, backward compatibility
            # with old distutils upload commands needs to be preserved
            raise NotImplementedError, "Unsupported file upload protocol"

        # figure the package name and version
        name = version = None
        if parameters.has_key('name'):
            name = parameters['name']
        if parameters.has_key('version'):
            version = parameters['version']
        if not name or not version:
            raise FormError, 'Name and version are required'

        # make sure the user has permission to do stuff
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            raise Forbidden, \
                "You are not allowed to edit '%s' package information"%name

        # verify the release exists
        if self.store.has_release(name, version):
            release_metadata = self.store.get_package(name, version)
            description = release_metadata['description']
        else:
            # auto-register the release...
            release_metadata = self.form_metadata(parameters)
            description = release_metadata.get('description')
            try:
                self.validate_metadata(release_metadata)
            except ValueError, message:
                raise FormError, message
            release_metadata['_pypi_hidden'] = False
            self.store.store_package(name, version, release_metadata)
            self.store.changed()

        # distutils handily substitutes blank descriptions with "UNKNOWN"
        if description == 'UNKNOWN':
            description = ''

        # verify we have enough information
        pyversion = 'source'
        content = filetype = md5_digest = comment = None
        if parameters.has_key('content'):
            content = parameters['content']
        if parameters.has_key('filetype'):
            filetype = parameters['filetype']
            if filetype == 'sdist':
                parameters['pyversion'] = 'source'
        if not content or not filetype:
            raise FormError, 'Both content and filetype are required'

        md5_digest = parameters['md5_digest']

        comment = parameters['comment']

        # python version?
        if parameters.get('pyversion', ''):
            pyversion = parameters['pyversion']
        elif filetype not in (None, 'sdist'):
            raise FormError, 'Python version is required for binary distribution uploads'

        # check for valid filenames
        if not hasattr(content, 'filename'):
            # I would say the only way this can happen is someone messing
            # with the form...
            raise FormError, 'invalid upload'
        filename = content.filename
        if not safe_filenames.match(filename):
            raise FormError, 'invalid distribution file'

        # check existing filename
        if self.store.has_file(name, version, filename):
            raise FormError, 'A file named "%s" already exists for ' \
                ' %s-%s. To fix problems with that file you' \
                ' should create a new release.'%(filename, name, version)

        # check for dodgy filenames
        if '/' in filename or '\\' in filename:
            raise FormError, 'invalid distribution file'

        # check whether file name matches package name
        prefix = safe_name(name.lower())
        if not safe_name(filename.lower()).startswith(prefix):
            raise FormError, 'The filename for %s must start with "%s" (any case)' % (name, prefix)

        # check for valid content-type
        mt = content.type or 'image/invalid'
        if mt.startswith('image/'):
            raise FormError, 'invalid distribution file'

        # grab content
        content = content.value

        if parameters.has_key('gpg_signature'):
            signature = parameters['gpg_signature']
            try:
                # If the signature is present, it may come
                # as an empty string, or as a file upload
                signature = signature.value
            except AttributeError:
                pass
        else:
            signature = None

        # nothing over 40M please
        if len(content) > 40*1024*1024:
            raise FormError, 'distribution file too large'
        if signature and len(signature) > 100*1024:
            raise FormError, 'invalid signature'

        # check the file for valid contents based on the type
        if not verify_filetype.is_distutils_file(content, filename, filetype):
            raise FormError, 'invalid distribution file'

        # Check whether signature is ASCII-armored
        if signature and not signature.startswith("-----BEGIN PGP SIGNATURE-----"):
            raise FormError, "signature is not ASCII-armored"

        # Determine whether we could use a README to fill out a missing
        # description
        if not description:
            description, desc_html = extractPackageReadme(content,
                filename, filetype)
            if description:
                self.store.set_description(name, version, description, desc_html,
                    from_readme=True)

        # digest content
        m = hashlib.md5()
        m.update(content)
        calc_digest = m.hexdigest()

        if not md5_digest:
            md5_digest = calc_digest
        elif md5_digest != calc_digest:
            self.fail(heading='MD5 digest mismatch',
                message='''The MD5 digest supplied does not match a
                digest calculated from the uploaded file (m =
                hashlib.md5(); m.update(content); digest =
                m.hexdigest())''')
            return

        self.store.add_file(name, version, content, md5_digest,
            filetype, pyversion, comment, filename, signature)
        self.store.changed()

        if response:
            self.handler.send_response(200, 'OK')
            self.handler.set_content_type('text/plain')
            self.handler.end_headers()
            self.wfile.write('OK\n')

    #
    # Documentation Upload
    #
    def doc_upload(self):
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to edit package information"

        # can't perform CSRF check as this is invoked by tools
        #self.csrf_check()

        # figure the package name and version
        name = version = None
        if self.form.has_key('name'):
            name = self.form['name']
        if not name:
            raise FormError, 'No package name given'

        # make sure the user has permission to do stuff
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            raise Forbidden, \
                "You are not allowed to edit '%s' package information"%name

        if not self.form.has_key('content'):
            raise FormError, "No file uploaded"

        try:
            data = self.form['content'].value
        except AttributeError:
            # error trying to get the .value *probably* means we didn't get
            # a file uploaded in the content element
            raise FormError, "No file uploaded"

        if len(data) > 20*1024*1024:
            raise FormError, "Documentation zip file is too large"
        data = cStringIO.StringIO(data)
        try:
            data = zipfile.ZipFile(data)
            members = data.namelist()
        except Exception,e:
            raise FormError, "Error uncompressing zipfile:" + str(e)

        if 'index.html' not in members:
            raise FormError, 'Error: top-level "index.html" missing in the zipfile'

        # Assume the file is valid; remove any previous data
        path = os.path.join(self.config.database_docs_dir,
                            name.encode('utf8'), "")
        if os.path.exists(path):
            shutil.rmtree(path)
        os.mkdir(path)
        try:
            for fname in members:
                fpath = os.path.normpath(os.path.join(path, fname))
                if not fpath.startswith(path):
                    raise ValueError, "invalid path name:"+fname
                if fname.endswith("/"):
                    if not os.path.isdir(fpath):
                        os.mkdir(fpath)
                    continue
                upperdirs = os.path.dirname(fpath)
                if not os.path.exists(upperdirs):
                    os.makedirs(upperdirs)
                outfile = open(os.path.join(path, fname), "wb")
                outfile.write(data.read(fname))
                outfile.close()
        except Exception, e:
            raise FormError, "Error unpacking zipfile:" + str(e)

        self.store.log_docs(name, version)
        self.store.changed()
        raise Redirect("http://packages.python.org/%s/" % name)

    #
    # Reverse download for Google AppEngine
    #
    def gae_file(self):
        host = self.form['host']
        secret = self.form['secret']
        gae.transfer(host, secret, self.config.database_files_dir)
        self.handler.send_response(204, 'Initiated')
        self.handler.end_headers()

    #
    # classifiers listing
    #
    def list_classifiers(self):
        ''' Just return the list of classifiers.
        '''
        c = '\n'.join([c['classifier'] for c in self.store.get_classifiers()])
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(c + '\n')

    #
    # User handling code (registration, password changing)
    #
    def user_form(self):
        ''' Make the user authenticate before viewing the "register" form.
        '''
        if not self.authenticated:
            raise Unauthorised, 'You must authenticate'
        self.register_form()

    def register_form(self, openid_fields = (), username='', email='', openid=''):
        ''' Throw up a form for registering.
        '''
        info = {'name': '', 'password': '', 'confirm': '', 'email': '',
                'gpg_keyid': '', 'openids': [], 'openid_fields': openid_fields,
                'openid': openid}
        if self.username:
            user = self.store.get_user(self.username)
            info['new_user'] = False
            info['owns_packages'] = bool(self.store.user_packages(self.username, True))
            info['name'] = user['name']
            info['email'] = user['email']
            info['action'] = 'Update details'
            info['gpg_keyid'] = user['gpg_keyid'] or ""
            info['title'] = 'User profile'
            info['openids'] = self.store.get_openids(self.username)
            info['sshkeys'] = self.store.get_sshkeys(self.username)
            self.nav_current = 'user_form'
        else:
            info['new_user'] = True
            info['name'] = username
            info['email'] = email
            info['action'] = 'Register'
            info['title'] = 'Manual user registration'
            self.nav_current = 'register_form'

        self.write_template('register.pt', **info)

    def user(self):
        ''' Register, update or validate a user.

            This interface handles one of three cases:
                1. completion of rego with One Time Key
                2. new user sending in name, password and email
                3. updating existing user details for currently authed user
        '''
        message = ''

        info = {}
        for param in 'name password email otk confirm gpg_keyid'.split():
            v = self.form.get(param, '').strip()
            if v:
                info[param] = v
            else:
                if param == "gpg_keyid":
                    info[param] = ""

        # validate email and gpg key syntax
        if info.has_key('email'):
            if not safe_email.match(info['email']):
                raise FormError, 'Email is invalid (ASCII only)'
            if '@' not in info['email'] or '.' not in info['email']:
                raise FormError, 'Email is invalid'
        gpgid = info.get('gpg_keyid', '')
        gpgid = gpgid.strip()
        if gpgid:
            if not re.match("^[A-Fa-f0-9]{8,8}$", gpgid):
                raise FormError, 'GPG key ID is invalid'

        # email requirement check
        if 'email' not in info and 'otk' not in info:
            raise FormError, "Clearing the email address is not allowed"

        if info.has_key('otk'):
            # finish off rego
            if self.store.get_otk(info['otk']):
                response = 'Error: One Time Key invalid'
            elif self.form.has_key('agree_shown'):
                # user has posted the form with the usage agreement
                if not self.form.has_key('agree'):
                    self.fail('You need to confirm the usage agreement.',
                              heading='User registration')
                    return
                # OK, delete the key
                self.store.delete_otk(info['otk'])
                self.write_template('message.pt', title='Registration complete',
                                    message='You are now registered.',
                                    url='%s?:action=login' % self.url_path,
                                    url_text='Proceed to login')
                return
            else:
                # user has clicked the link in the email -- show agreement form
                user = self.store.get_user_by_otk(info['otk'])
                self.write_template('confirm.pt', title='Confirm registration',
                                    otk=info['otk'], user=user)
                return
        elif self.username is None:
            nonce = None
            for param in 'name email'.split():
                if not info.has_key(param):
                    raise FormError, '%s is required'%param

            if 'password' not in info or 'confirm' not in info:
                if 'openid.assoc_handle' not in self.form:
                    raise FormError, 'password and confirm are required'
                info['password'] = ''.join([random.choice(store.chars) for x in range(32)])
                # Recheck OpenID response
                qs = {}
                for key, value in self.form.items():
                    qs[key] = [value.encode('utf-8')]
                try:
                    signed, claimed_id = openid2rp.verify(qs, self.store.discovered,
                                                          self.store.find_association,
                                                          self.store.check_nonce)
                except Exception, e:
                    return self.fail('OpenID response has been tampered with:'+repr(e))
                if 'response_nonce' in signed:
                    nonce = qs['openid.response_nonce'][0]
            else:
                claimed_id = None
                if not info.has_key('confirm') or info['password'] <> info['confirm']:
                    self.fail("password and confirm don't match", heading='Users')
                    return

            # validate a complete set of stuff
            # new user, create entry and email otk
            name = info['name']
            if not safe_username.match(name):
                raise FormError, 'Username is invalid (ASCII alphanum,.,_ only)'
            if self.store.has_user(name):
                self.fail('user "%s" already exists'%name,
                    heading='User registration')
                return
            olduser = self.store.get_user_by_email(info['email'])
            if olduser:
                raise FormError, 'You have already registered as user '+olduser['name']
            # we are about to commit the user; check the reply nonce
            if nonce and self.store.duplicate_nonce(nonce):
                return self.fail('replay attack detected')
            info['otk'] = self.store.store_user(name, info['password'],
                info['email'], info.get('gpg_keyid', ''))
            if claimed_id:
                self.store.associate_openid(name, claimed_id)
            info['url'] = self.config.url
            info['admin'] = self.config.adminemail
            self.send_email(info['email'], rego_message%info)
            response = 'Registration OK'
            message = ('You should receive a confirmation email to %s shortly. '
                       'To complete the registration process, visit the link '
                       'indicated in the email.') % info['email']

        else:
            self.csrf_check()

            # update details
            user = self.store.get_user(self.username)
            password = info.get('password', '').strip()
            if not password:
                # no password entered - leave it alone
                password = None
            else:
                # make sure the confirm matches
                if password != info.get('confirm', ''):
                    self.fail("password and confirm don't match",
                        heading='User profile')
                    return
            email = info.get('email', user['email'])
            gpg_keyid = info.get('gpg_keyid', user['gpg_keyid'])
            self.store.store_user(self.username, password, email, gpg_keyid)
            response = 'Details updated OK'

        self.write_template('message.pt', title=response, message=message)

    def addkey(self):
        if not self.authenticated:
            raise Unauthorised

        if "key" not in self.form:
            raise FormError, "missing key"

        self.csrf_check()

        key = self.form['key'].splitlines()
        for line in key[1:]:
            if line.strip():
                raise FormError, "Invalid key format: multiple lines"
        key = key[0].strip()
        if not key.startswith('ssh-dss') and not key.startswith('ssh-rsa'):
            raise FormError, "Invalid key format: does not start with ssh-dss or ssh-rsa"
        self.store.add_sshkey(self.username, key)
        self.store.commit()
        self.update_sshkeys()
        return self.register_form()

    def delkey(self):
        if not self.authenticated:
            raise Unauthorised

        if "id" not in self.form:
            raise FormError, "missing parameter"

        self.csrf_check()

        try:
            id = int(self.form["id"])
        except:
            raise FormError, "invalid ID"
        for key in self.store.get_sshkeys(self.username):
            if key['id'] == id:
                break
        else:
            raise Unauthorised, "not your key"
        self.store.delete_sshkey(id)
        self.store.commit()
        self.update_sshkeys()
        return self.register_form()

    def forgotten_password_form(self):
        ''' Enable the user to reset their password.
        '''
        self.write_template("password_reset.pt", title="Request password reset")

    def password_reset(self):
        """Reset the user's password and send an email to the address given.
        """
        def resend_otk():
            info = {'otk':self.store.get_otk(user['name']), 'url':self.config.url,
                    'admin':self.config.adminemail, 'email': user['email'],
                    'name':user['name']}
            self.send_email(info['email'], rego_message%info)
            response = 'Registration OK'
            message = 'You should receive a confirmation email shortly.'
            self.write_template('message.pt', title="Resending registration key",
                message='Email with registration key resent')

        if self.form.has_key('email') and self.form['email'].strip():
            email = self.form['email'].strip()
            user = self.store.get_user_by_email(email)
            if not user:
                self.fail('email address unknown to me')
                return
            if self.store.get_otk(user['name']):
                return resend_otk()
            pw = ''.join([random.choice(chars) for x in range(10)])
            self.store.store_user(user['name'], pw, user['email'], None)
            info = {'name': user['name'], 'password': pw,
                'email':user['email']}
            info['admin'] = self.config.adminemail
            self.send_email(email, password_message%info)
            self.write_template('message.pt', title="Request password reset",
                message='Email sent with new password')
        elif self.form.has_key('name') and self.form['name'].strip():
            name = self.form['name'].strip()
            user = self.store.get_user(name)
            if not user:
                self.fail('user name unknown to me')
                return
            if self.store.get_otk(user['name']):
                return resend_otk()
            info = {'name': user['name'], 'url': self.config.url,
                 'email': urllib.quote(user['email'])}
            info['admin'] = self.config.adminemail
            self.send_email(user['email'], password_change_message%info)
            self.write_template('message.pt', title="Request password reset",
                message='Email sent to confirm password change')
        else:
            self.write_template("password_reset.pt", title="Request password reset",
                retry=True)

    def delete_user(self):
        if not self.authenticated:
            raise Unauthorised

        if self.form.has_key('submit_ok'):
            self.csrf_check()
            # ok, do it
            self.store.delete_user(self.username)
            self.authenticated = self.loggedin = False
            self.username = self.usercookie = None
            return self.home()
        elif self.form.has_key('submit_cancel'):
            self.ok_message='Deletion cancelled'
            return self.home()
        else:
            message = '''You are about to delete the %s account<br />
                This action <em>cannot be undone</em>!<br />
                Are you <strong>sure</strong>?'''%self.username

            fields = [
                {'name': ':action', 'value': 'delete_user'},
            ]
            return self.write_template('dialog.pt', message=message,
                title='Confirm account deletion', fields=fields)

    def send_email(self, recipient, message):
        ''' Send an administrative email to the recipient
        '''
        smtp = smtplib.SMTP(self.config.mailhost)
        smtp.sendmail(self.config.adminemail, recipient, message)

    def packageURL(self, name, version):
        ''' return a URL for the link to display a particular package
        '''
        if not isinstance(name, str): name = name.encode('utf-8')
        if version is None:
            # changelog entry with no version
            version = ''
        else:
            if not isinstance(version, str): version = version.encode('utf-8')
            version = '/'+urllib.quote(version)
        return u'%s/%s%s'%(self.url_path, urllib.quote(name), version)

    def packageLink(self, name, version):
        ''' return a link to display a particular package
        '''
        if not isinstance(name, unicode): name = name.decode('utf-8')
        if not isinstance(version, unicode): version = version.decode('utf-8')
        url = self.packageURL(name, version)
        name = cgi.escape(name)
        version = cgi.escape(version)
        return u'<a href="%s">%s&nbsp;%s</a>'%(url, name, version)

    def mirrors(self):
        ''' display the list of mirrors
        '''
        options = {'title': 'PyPI mirrors'}
        self.write_template('mirrors.pt', **options)

    def daytime(self):
        # Mirrors are supposed to provide /last-modified,
        # but it doesn't make sense to do so for the master server
        '''display the current server time.
        '''
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain')
        self.handler.end_headers()
        self.wfile.write(time.strftime("%Y%m%dT%H:%M:%S\n", time.gmtime(time.time())))

    def openid(self):
        self.write_template('openid.pt', title='OpenID Login')

    def claim(self):
        '''Claim an OpenID.'''
        if not self.loggedin:
            return self.fail('You are not logged in')
        if 'openid_identifier' in self.form:
            kind, claimed_id = openid2rp.normalize_uri(self.form['openid_identifier'])
            if kind == 'xri':
                return self.fail('XRI resolution is not supported')
            res = openid2rp.discover(claimed_id)
            if not res:
                return self.fail('Discovery failed. If you think this is in error, please submit a bug report.')
            stypes, op_endpoint, op_local = res
            if not op_local:
                op_local = claimed_id
            try:
                assoc_handle = self.store.get_session_for_endpoint(op_endpoint, stypes)
            except ValueError, e:
                return self.fail('Cannot establish OpenID session: ' + str(e))
            return_to = self.config.url+'?:action=openid_return'
            url = openid2rp.request_authentication(stypes, op_endpoint, assoc_handle, return_to, claimed_id, op_local)
            self.store.commit()
            raise RedirectTemporary(url)
        if not self.form.has_key("provider"):
            return self.fail('Missing parameter')
        for p in providers:
            if p[0] == self.form['provider']:
                break
        else:
            return self.fail('Unknown provider')
        stypes, url, assoc_handle = self.store.get_provider_session(p)
        return_to = self.config.url+'?:action=openid_return'
        url = openid2rp.request_authentication(stypes, url, assoc_handle, return_to)
        self.store.commit()
        raise RedirectTemporary(url)

    def openid_return(self):
        '''Return from OpenID provider.'''
        qs = cgi.parse_qs(self.env['QUERY_STRING'])
        if 'openid.mode' not in qs:
            # Not an indirect call: treat it as RP discovery
            return self.rp_discovery()
        mode = qs['openid.mode'][0]
        if mode == 'cancel':
            return self.fail('Login cancelled')
        if mode == 'error':
            return self.fail('OpenID login failed: '+qs['openid.error'][0])
        if mode != 'id_res':
            return self.fail('OpenID login failed')
        try:
            signed, claimed_id = openid2rp.verify(qs, self.store.discovered,
                                                  self.store.find_association,
                                                  self.store.check_nonce)
        except Exception, e:
            return self.fail('Login failed:'+repr(e))

        if 'response_nonce' in signed:
            nonce = qs['openid.response_nonce'][0]
        else:
            # OpenID 1.1
            nonce = None

        user = self.store.get_user_by_openid(claimed_id)
        # Three cases: logged-in user claimed some ID,
        # new login, or registration
        if self.loggedin:
            # claimed ID
            if user:
                return self.fail('OpenID is already claimed')
            if nonce and self.store.duplicate_nonce(nonce):
                return self.fail('replay attack detected')
            self.store.associate_openid(self.username, claimed_id)
            self.store.commit()
            return self.register_form()
        if user:
            # Login
            if nonce and self.store.duplicate_nonce(nonce):
                return self.fail('replay attack detected')
            self.store.commit()
            self.username = user['name']
            self.loggedin = self.authenticated = True
            self.usercookie = self.store.create_cookie(self.username)
            self.store.get_token(self.username)
            return self.home()
        # Fill openid response fields into register form as hidden fields
        del qs[':action']
        openid_fields = []
        for key, value in qs.items():
            openid_fields.append((key, value[0]))
        # propose email address based on response
        email = openid2rp.get_email(qs)
        # propose user name based on response
        username = openid2rp.get_username(qs)
        if isinstance(username, tuple):
            username = '.'.join(username)
        elif email and not username:
            username = email.split('@')[0]
        else:
            # suggest OpenID host name as username
            username = urlparse.urlsplit(claimed_id)[1]
            if ':' in username:
                username = username.split(':')[0]
            if '@' in username:
                username = username.rsplit('@', 1)[0]
            if not username:
                username = "nonamegiven"
        username = username.replace(' ','.')
        username = re.sub('[^a-zA-Z0-9._]','',username)
        error = 'Please choose a username to complete registration'
        if self.store.has_user(username):
            suffix = 2
            while self.store.has_user("%s_%d" % (username, suffix)):
                suffix += 1
            username = "%s_%d" % (username, suffix)
        return self.register_form(openid_fields, username, email, claimed_id)

    def dropid(self):
        if not self.loggedin:
            return self.fail('You are not logged in')
        if 'openid' not in self.form:
            raise FormError, "ID missing"
        openid = self.form['openid']
        for i in self.store.get_openids(self.username):
            if openid == i['id']:break
        else:
            raise Forbidden, "You don't own this ID"
        self.store.drop_openid(openid)
        return self.register_form()

    def rp_discovery(self):
        payload = '''<xrds:XRDS
                xmlns:xrds="xri://$xrds"
                xmlns="xri://$xrd*($v*2.0)">
                <XRD>
                     <Service priority="1">
                              <Type>http://specs.openid.net/auth/2.0/return_to</Type>
                              <URI>%s</URI>
                     </Service>
                </XRD>
                </xrds:XRDS>
        ''' % (self.config.url+'?:action=openid_return')
        self.handler.send_response(200)
        self.handler.send_header("Content-type", 'application/xrds+xml')
        self.handler.send_header("Content-length", str(len(payload)))
        self.handler.end_headers()
        self.handler.wfile.write(payload)

    def get_providers(self):
        res = []
        for r in providers:
            r = Provider(*r)
            r.login = "%s?:action=login&provider=%s" % (self.url_path, r.name)
            r.claim = "%s?:action=claim&provider=%s" % (self.url_path, r.name)
            res.append(r)
        return res

    def update_sshkeys(self):
        if not self.config.sshkeys_update:
            return
        p = subprocess.Popen([self.config.sshkeys_update],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        stdout = p.communicate()[0]
        if p.returncode != 0:
            raise FormError, "Key processing failed. Please contact the administrator. Detail: "+stdout

    #
    # OpenID Provider
    #
    def openid_discovery(self):
        """Return an XRDS document containing an OpenID provider endpoint URL."""
        payload = '''<xrds:XRDS
                xmlns:xrds="xri://$xrds"
                xmlns="xri://$xrd*($v*2.0)">
                <XRD>
                    <Service priority="0">
                      <Type>http://specs.openid.net/auth/2.0/server</Type>
                      <Type>http://specs.openid.net/auth/2.0/signon</Type>
                      <URI>%s</URI>
                    </Service>
                </XRD>
            </xrds:XRDS>
        ''' % (self.config.url+'?:action=openid_endpoint')
        self.handler.send_response(200)
        self.handler.send_header("Content-type", 'application/xrds+xml')
        self.handler.send_header("Content-length", str(len(payload)))
        self.handler.end_headers()
        self.handler.wfile.write(payload)

    def openid_user(self, user):
        """Return an XRDS document containing an OpenID provider endpoint URL."""
        payload = '''<xrds:XRDS
                xmlns:xrds="xri://$xrds"
                xmlns="xri://$xrd*($v*2.0)">
                <XRD>
                    <Service priority="0">
                      <Type>http://specs.openid.net/auth/2.0/signon</Type>
                      <URI>%s</URI>
                    </Service>
                </XRD>
            </xrds:XRDS>
        ''' % (self.config.url+'?:action=openid_endpoint')
        self.handler.send_response(200)
        self.handler.send_header("Content-type", 'application/xrds+xml')
        self.handler.send_header("Content-length", str(len(payload)))
        self.handler.end_headers()
        self.handler.wfile.write(payload)

    def openid_endpoint(self):
        """Handle OpenID requests"""
        orequest = self.oid_server.decodeRequest(self.form)
        if not orequest or orequest is None:
            payload='''This is an OpenID server'''
            self.handler.send_response(200)
            self.handler.send_header("Content-type", 'text/plain')
            self.handler.send_header("Content-length", str(len(payload)))
            self.handler.end_headers()
            self.handler.wfile.write(payload)
            return
        if orequest.mode in ['checkid_immediate', 'checkid_setup']:
            if self.openid_is_authorized(orequest):
                answer = orequest.answer(True, identity=self.openid_user_url())
                return self.openid_response(answer)
            elif orequest.immediate:
                return self.openid_response(orequest.answer(False))
            else:
                self.openid_decide_page(orequest)
        elif orequest.mode in ['associate', 'check_authentication']:
            self.openid_response(self.oid_server.handleRequest(orequest))
        else:
            raise OpenIDError, "Unknown mode: %s" % orequest.mode

    def openid_decide_page(self, orequest):
        """
        The page that asks the user if they really want to trust this trust_root
        If they are NOT logged intp PyPI, show the landing page so the user
        understands why it has failed and they need to login to PyPI before
        attempting again. This is done rather than presenting PyPI login page
        to reduce chance of phishing.
        """
        if not self.authenticated:
            self.write_template('openid_notloggedin.pt',
                                title="OpenID login attempt")
            return

        if orequest.identity == "http://specs.openid.net/auth/2.0/identifier_select":
            pending_id = self.openid_user_url()
        else:
            pending_id = orequest.identity

        orequest_args=orequest.message.toPostArgs()
        del orequest_args[':action']
        # They are logged in - ask if they want to trust this root
        self.write_template('openid_decide.pt', title="Trust this site?",
                            url_path="%s/?:action=openid_decide_post" % self.config.url,
                            orequest=orequest_args,
                            mode=orequest.mode,
                            identity=self.username,
                            return_to=orequest.return_to,
                            trust_root=orequest.trust_root,
                            pending_id = pending_id)

    def openid_decide_post(self):
        """Handle POST request from decide form"""
        if self.env['REQUEST_METHOD'] != "POST":
            raise OpenIDError, "OpenID request must be a POST"

        from openid.message import Message
        del self.form[':action']
        message = Message.fromPostArgs(self.form)
        orequest = OpenIDServer.CheckIDRequest.fromMessage(message, self.oid_server.op_endpoint)

        if self.form.has_key('allow'):
            answer = orequest.answer(True, identity=self.openid_user_url())
            return self.openid_response(answer)
        elif self.form.has_key('allow_always'):
            answer = orequest.answer(True, identity=self.openid_user_url())
            self.store.set_openid_trustedroot(self.username, orequest.trust_root)
            self.store.commit()
            return self.openid_response(answer)
        elif self.form.has_key('no_thanks'):
            answer = orequest.answer(False)
            return self.openid_response(answer)
        else:
            raise OpenIDError, "OpenID post request failure"

    def openid_response(self, oresponse):
        """Convert a webresponse from the OpenID library into a
        WebUI http response"""
        webresponse = self.oid_server.encodeResponse(oresponse)
        if webresponse.code == 301:
            raise Redirect, str(webresponse.headers['location'])
        elif webresponse.code == 302:
            raise RedirectFound, str(webresponse.headers['location'])

        self.handler.send_response(webresponse.code)
        for key, value in webresponse.headers.items():
            self.handler.send_header(key, str(value))
        self.handler.end_headers()
        self.handler.wfile.write(webresponse.body)

    def openid_is_authorized(self, orequest):
        """
        This should check that they own the given identity,
        and that the trust_root is in their whitelist of trusted sites.
        """
        identity = orequest.identity
        if not self.authenticated:
            return False
        if identity == 'http://specs.openid.net/auth/2.0/identifier_select':
            identity = self.openid_user_url()
        id_prefix = self.config.scheme_host + "/id/"
        if not identity.startswith(id_prefix):
            return False
        username = identity[len(id_prefix):]
        if username == self.username:
            if self.store.check_openid_trustedroot(self.username, orequest.trust_root):
                return True
            else:
                return False
        # identity is not owned by user so decline the request
        return False

    def openid_user_url(self):
        if self.authenticated:
            return "%s/id/%s" % (self.config.scheme_host, self.username)
        else:
            return None

    #
    # OAuth
    #
    def run_oauth(self):
        if self.env.get('HTTPS') != 'on':
            raise NotFound('HTTPS must be used to access this URL')

        path = self.env.get('PATH_INFO')

        if path == '/request_token':
            self.oauth_request_token()
        elif path == '/access_token':
            self.oauth_access_token()
        elif path == '/authorise':
            self.oauth_authorise()
        elif path == '/add_release':
            self.oauth_add_release()
        elif path == '/upload':
            self.oauth_upload()
        elif path == '/docupload':
            self.oauth_docupload()
        elif path == '/test':
            self.oauth_test_access()
        else:
            raise NotFound()

    def _oauth_request(self):
        uri = self.url_machine + self.env['REQUEST_URI']
        return oauth.OAuthRequest.from_request(self.env['REQUEST_METHOD'],
            uri, dict(Authorization=self.env['HTTP_AUTHORIZATION']),
            self.form)

    def _oauth_server(self):
        data_store = store.OAuthDataStore(self.store)
        o = oauth.OAuthSignatureMethod_HMAC_SHA1()
        signature_methods = {o.get_name(): o}
        return oauth.OAuthServer(data_store, signature_methods)

    def oauth_request_token(self):
        s = self._oauth_server()
        r = self._oauth_request()
        token = s.fetch_request_token(r)
        self.store.commit()
        self.write_plain(str(token))

    def oauth_access_token(self):
        s = self._oauth_server()
        r = self._oauth_request()
        token = s.fetch_access_token(r)
        self.store.commit()
        self.write_plain(str(token))

    def oauth_authorise(self):
        if 'oauth_token' not in self.form:
            raise FormRequest('oauth_token and oauth_callback are required')
        if not self.authenticated:
            self.write_template('oauth_notloggedin.pt',
                title="OAuth authorisation attempt")
            return

        oauth_token = self.form['oauth_token']
        oauth_callback = self.form['oauth_callback']

        ok = self.form.get('ok')
        cancel = self.form.get('cancel')

        s = self._oauth_server()

        if not ok and not cancel:
            description = s.data_store._get_consumer_description(request_token=oauth_token)
            action_url = self.url_machine + '/oauth/authorise'
            return self.write_template('oauth_authorise.pt',
                title='PyPI - the Python Package Index',
                action_url=action_url,
                oauth_token=oauth_token,
                oauth_callback=oauth_callback,
                description=description)

        if '%3A' in oauth_callback:
            oauth_callback = urllib.unquote(oauth_callback)

        if not ok:
            raise RedirectTemporary(oauth_callback)

        # register the user against the request token
        s.authorize_token(oauth_token, self.username)

        # commit all changes now
        self.store.commit()

        url = oauth_callback + '?oauth_token=%s'%oauth_token
        raise RedirectTemporary(url)

    def _parse_request(self):
        '''Read OAuth access request information from the request.

        Return the consumer (OAuthConsumer instance), the access token
        (OAuthToken instance), the parameters (may include non-OAuth parameters
        accompanying the request) and the user account number authorized by the
        access token.
        '''
        s = self._oauth_server()
        r = self._oauth_request()
        consumer, token, params = s.verify_request(r)
        user = s.data_store._get_user(token)
        # recognise the user as accessing during this request
        self.username = user
        self.store.set_user(user, self.env['REMOTE_ADDR'], False)
        self.authenticated = True
        return consumer, token, params, user

    def oauth_test_access(self):
        '''A resource that is protected so access without an access token is
        disallowed.
        '''
        consumer, token, params, user = self._parse_request()
        message = 'Access allowed for %s (ps. I got params=%r)'%(user, params)
        self.write_plain(message)

    def oauth_add_release(self):
        '''Add a new release.

        Returns "OK" if all is well otherwise .. who knows (TODO this needs to
        be clarified and cleaned up).
        '''
        consumer, token, params, user = self._parse_request()
        self.submit(params, False)
        self.write_plain('OK')

    def oauth_upload(self):
        '''Upload a file for a package release.
        '''
        consumer, token, params, user = self._parse_request()
        self.file_upload(False, params)
        self.write_plain(message)

    def oauth_docupload(self):
        '''Upload a documentation bundle.
        '''
        consumer, token, params, user = self._parse_request()
        message = 'Access allowed for %s (ps. I got params=%r)'%(user, params)
        self.write_plain(message)

