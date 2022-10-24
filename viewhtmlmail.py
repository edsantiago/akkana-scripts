#! /usr/bin/env python

# Take an mbox HTML message (e.g. from mutt), split it
# and rewrite it so it can be viewed in an external browser.
# Can be run from within a mailer like mutt, or independently
# on a single message file.
#
# Usage: viewhtmlmail
#
# Inspired by John Eikenberry <jae@zhar.net>'s view_html_mail.sh
# which sadly no longer works, at least with mail from current Apple Mail.
#
# Copyright 2013-2018 by Akkana Peck. Share and enjoy under the GPL v2 or later.
# Changes:
#   Holger Klawitter 2014: create a secure temp file and avoid temp mbox
#   Antonio Terceiro 2018: Allow piping directly from mutt.

# To use it from mutt, put the following lines in your .muttrc:
# macro  index  <F10>  "<pipe-message>~/bin/viewhtmlmail\n" "View HTML in browser"
# macro  pager  <F10>  "<pipe-message>~/bin/viewhtmlmail\n" "View HTML in browser"

# TESTING: Use the email file in test/files/htmlmail.eml.

import os, sys
import re
import time
import shutil
import email, mimetypes
from email.parser import BytesParser
from email.policy import default as default_policy
import subprocess


################################################
# Some prefs:

DEBUG = False

# If IMAGE_VIEWER is set, a message that has no multipart/related
# images will use the image viewer rather than a browser window
# for images. To use a browser, set IMAGE_VIEWER = None.
IMAGE_VIEWER = "pho"
# IMAGE_VIEWER = None

USE_WVHTML_FOR_DOC = False

# How many seconds do we need to wait for unoconv?
# It defaults to 6, but on a 64-bit machine that's not nearly enough.
# Even 10 often isn't enough.
UNOCONV_STARTUP_TIME = "14"

# Does the browser need a one-time argument for bringing up an initial window,
# like Firefox's -private-window -new-instance ?
BROWSER_FIRST_ARGS = []

# Arguments to use on subsequent calls
BROWSER_ARGS = []

# What browser to use.
# Quickbrowse is my script, a little python-qt5webengine script
# that comes up a lot faster than firefox and tries to be more anonymous.
# But it's Qt5 only; I haven't managed to get it working in Qt6.
USE_QUICKBROWSE = False

# Qutebrowser is a well respected fast and lightweight browser,
# which hopefully with the given arguments is as private as quickbrowse.
USE_QUTEBROWSER = True

if USE_QUTEBROWSER:
    BROWSER = "qutebrowser"

    # Arguments for calling the browser on the index page:
    # Args recommended by FAQ #1 https://www.qutebrowser.org/doc/faq.html
    # but don't use --temp-basedir, as that gives a separate basedir
    # for each run and so makes it impossible to use tabs in a single window.
    # To specify initial window size, add args
    # "--qt-arg", "geometry", "1024x768" (or whatever)
    # or make a rule for your window manager.
    BROWSER_FIRST_ARGS = [ "--target", "private-window",
                           "--basedir", "/tmp/mailattachments",
                           "-s", "content.dns_prefetch", "false",
                           "-s", "content.javascript.enabled", "false"
                          ]
    BROWSER_ARGS = [ "--target", "tab-bg",
                     "--basedir", "/tmp/mailattachments",
                     # Don't need to specify privacy, prefetch or JS
                     # because it's being opened in a window that
                     # already has those settings, using the same configdir.
                   ]

    # qutebrowser runs in foreground
    BROWSER_BACKGROUND = True

    CONVERT_PDF_TO_HTML = True

else:
    if USE_QUICKBROWSE:
        BROWSER = "quickbrowse"

        # Arguments for calling the browser on the index page:
        BROWSER_FIRST_ARGS = []
        # Browser argument to precede new tabs:
        BROWSER_ARGS = [ "--new-tab" ]

        # Quickbrowse normally backgrounds itself, so we don't have to.
        BROWSER_BACKGROUND = False

        # If you have qpdfhtml installed, no need to convert PDF.
        # Otherwise, set this to True.
        CONVERT_PDF_TO_HTML = False

    else:    # fall back to Firefox in private browsing mode
        BROWSER = "firefox"

        # For the index page, bring up a new, private window:
        BROWSER_FIRST_ARGS = [ "-P", "Default", "-private-window",
                               "-new-instance" ]
        # Subsequent tabs should be tabs in the private window
        BROWSER_ARGS = [ "-P", "Default", "-private-window" ]
        # Firefox doesn't run in the background.
        BROWSER_BACKGROUND = True

        # Not clear what to do here: Firefox has a built-in PDF viewer,
        # but for some mime types it can't figure out that it should use it,
        # and there's no way to find out from outside the process.
        CONVERT_PDF_TO_HTML = False

# Seconds to wait between refreshes when waiting for translated content
REDIRECT_TIMEOUT = 2

# End global prefs
################################################


def find_first_maildir_file(maildir):
    """Maildir: inside /tmp/mutttmpbox, mutt creates another level of
       directory, so the file will be something like /tmp/mutttmpbox/cur/1.
       So recurse into directories until we find an actual mail file.
       Return a full path to the filename.
    """
    for root, dirs, files in os.walk(maildir):
        for f in files:
            if not f.startswith('.'):
                return os.path.join(root, f)
    return None

# Sanitize a filename to make sure there's nothing dangerous, like ../
def sanitize_filename(badstr):
    return ''.join([x for x in badstr if x.isalpha() or x.isdigit()
                    or x in '-_.'])

def view_html_message(f, tmpdir):
    # Note: the obvious way to read a message is
    #   with open(f) as fp: msg = email.message_from_file(fp)
    # What the docs don't tell you is that that gives you an
    # email.message.Message, which is limited and poorly documented;
    # all the documentation assumes you have an email.message.EmailMessage,
    # but to get that you need the more complicated BytesParser method below.
    # The policy argument to BytesParser is mandatory: without it,
    # again, you'll get a Message and not an EmailMessage.
    if f:
        if os.path.isdir(f):
            # Maildir: f is a maildir like /tmp/mutttmpbox,
            # and inside it, for some reason, mutt creates another
            # level of directory named either cur or new
            # depending on whether the message is already marked read.
            # So we have to open the first file inside either cur or new.
            # In case mutt changes this behavior, let's take the first
            # non-dotfile inside the first non-dot directory.
            msg = None
            for maildir in os.listdir(f):
                with open(find_first_maildir_file(f)) as fp:
                    msg = email.message_from_string(fp.read())
                    break
        else:
            # Mbox format: we assume there's only one message in the mbox.
            with open(f, 'rb') as fp:
                # msg = email.message_from_string(fp.read())
                msg = BytesParser(policy=default_policy).parse(fp)
    else:
        msg = email.message_from_string(sys.stdin.read())

    counter = 1
    filename = None
    filenames = set()
    subfiles = {}    # A dictionary mapping content-id to [filename, part]
    html_parts = []

    # For debugging:
    def print_part(part):
        print("*** part:")   # parts are type email.message.Message
        print("  content-type:", part.get_content_type())
        print("  content-disposition:", part.get_content_disposition())
        print("  content-id:", part.get('Content-ID'))
        print("  filename:", part.get_filename())
        print("  is_multipart?", part.is_multipart())

    def print_structure(msg, indent=0):
        """Iterate over an EmailMessage, printing its structure"""
        indentstr = ' ' * indent
        for part in msg.iter_parts():
            print("%scontent-type:" % indentstr, part.get_content_type())
            print("  content-id:", part.get('Content-ID'))
            print("%scontent-disposition:" % indentstr,
                  part.get_content_disposition())
            print("%sfilename:" % indentstr, part.get_filename())
            print("%sis_multipart?" % indentstr, part.is_multipart())
            print_structure(part, indent=indent+2)
            print()

    if DEBUG:
        print_structure(msg)

    for part in msg.walk():
        if DEBUG:
            print()
            print_part(part)

        # multipart/* are just containers
        #if part.get_content_maintype() == 'multipart':
        if part.is_multipart() or part.get_content_type == 'message/rfc822':
            continue

        # Get the content id.
        # Mailers may use Content-Id or Content-ID (or, presumably, various
        # other capitalizations). So we can't just look it up simply.
        content_id = None
        for k in list(part.keys()):
            if k.lower() == 'content-id':
                # Remove angle brackets, if present.
                # part['Content-Id'] is unmutable -- attempts to change it
                # are just ignored -- so copy it to a local mutable string.
                content_id = part[k]
                if content_id.startswith('<') and content_id.endswith('>'):
                    content_id = content_id[1:-1]

                counter += 1

                break     # no need to look at other keys

        if part.get_content_subtype() == 'html':
            if DEBUG:
                print("Found an html part")
                if html_parts:
                    print("Eek, more than one html part!")
            html_parts.append(part)

        elif not content_id:
            if DEBUG:
                print("No Content-Id")
            pass

        # Use the filename provided if possible, otherwise make one up.
        filename = part.get_filename()

        if filename:
            filename = sanitize_filename(filename)
        else:
            # if DEBUG:
            #     print("No filename; making one up")
            ext = mimetypes.guess_extension(part.get_content_type())
            if not ext:
                # Use a generic bag-of-bits extension
                ext = '.bin'
            if content_id:
                filename = sanitize_filename('cid%s%s' % (content_id, ext))
            else:
                filename = 'part-%03d%s' % (counter, ext)

        # Some mailers, like gmail, will attach multiple images to
        # the same email all with the same filename, like "image.png".
        # So check whether we have to uniquify the names.

        if filename in filenames:
            orig_basename, orig_ext = os.path.splitext(filename)
            counter = 0
            while filename in filenames:
                counter += 1
                filename = "%s-%d%s" % (orig_basename, counter, orig_ext)

        filenames.add(filename)

        filename = os.path.join(tmpdir, filename)

        # Now save content to the filename, and remember it in subfiles
        if content_id:
            subfiles[content_id] = [ filename, part ]
        with open(filename, 'wb') as fp:
            fp.write(part.get_payload(decode=True))
            if DEBUG:
                print("wrote", filename)

        # print "%10s %5s %s" % (part.get_content_type(), ext, filename)

    if DEBUG:
        print("\nsubfiles now:", subfiles)
        print()

    # We're done saving the parts. It's time to save the HTML part(s),
    # with img tags rewritten to refer to the files we just saved.
    embedded_parts = []
    first_browser = True
    for i, html_part in enumerate(html_parts):
        htmlfile = os.path.join(tmpdir, "viewhtml%02d.html" % i)
        fp = open(htmlfile, 'wb')

        # htmlsrc should be a string.
        # html_parts[i].get_payload() returns string, but it's apparently
        # in straight unicode and doesn't reflect the message's charset.
        # html_part.get_payload(decode=True) returns bytes,
        # which (I think) have been decoded as far as email transfer
        # (e.g. Content-Encoding: base64), which is not the same thing
        # as charset decoding.
        # (None of this is documented in the python3 email module;
        # there's no mention of get_payload() at all. Sigh.)

        # This works, but assumes UTF-8:
        # htmlsrc = html_part.get_payload(decode=True).decode('utf-8', "replace")

        # but it's probably better to use the system encoding:
        htmlsrc = html_part.get_payload(decode=True).decode(errors="replace")

        # Substitute all the filenames for content_ids:
        for sf_cid in subfiles:
            # Yes, yes, I know:
            # https://stackoverflow.com/questions/1732348/regex-match-open-tags-except-xhtml-self-contained-tags/
            # but eventually this script will be integrated with viewmailattachments
            # (which uses BeautifulSoup) and that problem will go away.
            if DEBUG:
                print("Replacing cid", sf_cid, "with", subfiles[sf_cid][0])
            newhtmlsrc = re.sub('cid: ?' + sf_cid,
                             'file://' + subfiles[sf_cid][0],
                             htmlsrc, flags=re.IGNORECASE)
            if sf_cid not in embedded_parts and newhtmlsrc != htmlsrc:
                embedded_parts.append(sf_cid)
            htmlsrc = newhtmlsrc

        fp.write(htmlsrc.encode())
        fp.close()
        if DEBUG:
            print("Wrote", htmlfile)

    # Now we have the file. Call a browser on it.
        if DEBUG:
            print("Calling browser for file://%s" % htmlfile)
        cmd = [ BROWSER ]
        if first_browser:
            cmd += BROWSER_FIRST_ARGS
            first_browser = False
        else:
            cmd += BROWSER_ARGS

        cmd.append("file://" + htmlfile)
        if DEBUG:
            print("Calling in background: %s" % ' '.join(cmd))
        mysubprocess.call_bg(cmd)

    # Done with htmlparts.
    # Now handle any parts that aren't embedded inside HTML parts.
    # This includes conversions from Word or PDF, but also image attachments.
    if DEBUG:
        print()
        print("subfiles:", subfiles)
        print("Parts already embedded:", embedded_parts)
        print("\n************************************\n")
    for sfid in subfiles:
        if DEBUG:
            print("\nPart:", subfiles[sfid][0])
        part = subfiles[sfid][1]
        partfile = subfiles[sfid][0]    # full path
        fileparts = os.path.splitext(partfile)

        if sfid in embedded_parts:
            if DEBUG:
                print(partfile, "was embedded in html")
            continue

        if part.get_content_maintype() == "application":
            htmlfilename = fileparts[0] + ".html"

            if DEBUG:
                print("Application subtype:", part.get_content_subtype())
            if part.get_content_subtype() == "msword" and USE_WVHTML_FOR_DOC:
                mysubprocess.call(["wvHtml", partfile, htmlfilename])
                delayed_browser(htmlfilename)
                continue

            if part.get_content_subtype() == \
                 "vnd.openxmlformats-officedocument.wordprocessingml.document" \
                 or part.get_content_subtype() == "msword" \
                 or part.get_content_subtype() == "vnd.oasis.opendocument.text":
                mysubprocess.call(["unoconv", "-f", "html",
                                      "-T", UNOCONV_STARTUP_TIME,
                                      "-o", htmlfilename, partfile])
                delayed_browser(htmlfilename)
                continue

            # unoconv conversions from powerpoint to HTML drop all images.
            # Try converting to PDF instead:
            if part.get_content_subtype() == "vnd.ms-powerpoint" \
                 or part.get_content_subtype() == \
              "vnd.openxmlformats-officedocument.presentationml.presentation":
                pdffile = fileparts[0] + ".pdf"
                mysubprocess.call(["unoconv", "-f", "pdf",
                                      "-o", pdffile, partfile])
                partfile = pdffile

            if part.get_content_subtype() == "pdf" or partfile.endswith(pdf):
                if CONVERT_PDF_TO_HTML:
                    mysubprocess.call(["pdftohtml", "-s", partfile])

                    # But pdftohtml is idiotic about output filename
                    # and won't let you override it:
                    delayed_browser(fileparts[0] + "-html.html")
                else:
                    delayed_browser(partfile)

def delayed_browser(htmlfile):
    # Call up the browser window right away,
    # so the user can see something is happening.
    # Firefox, alas, has no way from the commandline of calling up
    # a new private window with content, then replacing that content.
    # So we'll create a file that refreshes, so that when content is ready,
    # it can redirect to the first content page.
#     global delayed_tabs
#     def write_to_index(outfile, msg, timeout_secs, redirect_url):
#         if not redirect_url:
#             redirect_url = "file://%s" % outfile
#         ofp = open(outfile, "w")
#         ofp.write('''<html><head>
# <meta content="utf-8" http-equiv="encoding">
# <meta http-equiv="content-type" content="text/html; charset=UTF-8">
# <meta http-equiv="refresh" content="%d;URL=%s">
# </head><body>
# <br><br><br><br><br><br><big><big>%s</big></big>
# </body></html>
# ''' % (timeout_secs, redirect_url, msg))
#         ofp.close()

#     write_to_index(pleasewait_file, "Please wait ...", REDIRECT_TIMEOUT, None)

    cmd = [ BROWSER ]
    cmd += BROWSER_ARGS
    # cmd.append("file://" + pleasewait_file)
    cmd.append("file://" + htmlfile)
    if DEBUG:
        print("Calling:", cmd)
    mysubprocess.call_bg(cmd)


# For debugging:
class mysubprocess:
    @staticmethod
    def call(arr):
        if DEBUG:
            print("\n\n================\n=== Calling: %s" % str(arr))
        subprocess.call(arr)

    @staticmethod
    def call_bg(arr):
        if DEBUG:
            print("\n\n================\n=== Calling in background: %s"
                  % str(arr))
        subprocess.Popen(arr, shell=False,
                         stdin=None, stdout=None, stderr=None)


if __name__ == '__main__':
    import tempfile

    tmpdir = tempfile.mkdtemp()
    if len(sys.argv) > 1:
        for f in sys.argv[1:]:
            view_html_message(f, tmpdir)
    else:
        stdin = '%s/.stdin' % tmpdir
        with open(stdin, 'w') as f:
            f.write(sys.stdin.read())
        view_html_message(stdin, tmpdir)
