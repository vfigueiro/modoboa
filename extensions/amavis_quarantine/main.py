# -*- coding: utf-8 -*-

"""
"""
from django.http import HttpResponseRedirect, HttpResponse
from django.shortcuts import get_object_or_404, render_to_response
from django.template.loader import render_to_string
from django.template import Template
from django.utils import simplejson
from django.conf.urls.defaults import *
from django.utils.translation import ugettext as _
from django.core.urlresolvers import reverse
from django.core.paginator import Paginator
from django.contrib.auth.decorators \
    import login_required
from mailng.lib import events
from mailng.lib import _render, _ctx_ok, _ctx_ko, decode
from mailng.lib import db
from mailng.admin.models import Mailbox
from datetime import datetime
import email
import re
import os

def init():
    events.register("UserMenuDisplay", menu)

def urls():
    return (r'^mailng/quarantine/', 
            include('mailng.extensions.amavis_quarantine.urls'))

def menu(**kwargs):
    return [
        {"name" : _("Quarantine"),
         "url" : reverse(index),
         "img" : "/static/pics/quarantine.png"}
        ]

@login_required
def index(request, message=None):
    if not request.user.is_superuser:
        mb = Mailbox.objects.get(user=request.user.id)
        filter = "AND maddr.email='%s'" % mb.full_address
    else:
        filter = ""
    conn = db.getconnection("amavis_quarantine")
    status, cursor = db.execute(conn, """
SELECT msgs.from_addr, maddr.email, msgs.subject, msgs.content, quarantine.mail_id,
       msgs.time_num, msgs.content
FROM quarantine, maddr, msgrcpt, msgs
WHERE quarantine.mail_id=msgrcpt.mail_id
AND msgrcpt.rid=maddr.id
AND msgrcpt.mail_id=msgs.mail_id
AND quarantine.chunk_ind=1
%s
ORDER BY msgs.time_num DESC
""" % filter)
    emails = []
    rows = cursor.fetchall()
    for row in rows:
        emails.append({"from" : row[0], "to" : row[1], 
                       "subject" : row[2], "content" : row[3],
                       "mailid" : row[4], "time" : datetime.fromtimestamp(row[5]),
                       "type" : row[6]})
    
    paginator = Paginator(emails, 10)
    try:
        pagenum = int(request.GET.get('page', '1'))
    except ValueError:
        pagenum = 1
    page = paginator.page(pagenum)
    return _render(request, 'amavis_quarantine/index.html', {
            "count" : len(rows), "emails" : page, 
            "current_page" : pagenum, "last_page" : paginator._get_num_pages()
            })

@login_required
def viewmail(request, mail_id):
    conn = db.getconnection("amavis_quarantine")
    status, cursor = db.execute(conn, """
SELECT mail_text
FROM quarantine
WHERE quarantine.mail_id='%s'
""" % mail_id)
    content = ""
    for part in cursor.fetchall():
        content += part[0]
    msg = email.message_from_string(content)

    html_body = ""
    text_body = ""
    for part in msg.walk():
        print part.get_content_type()
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get_content_type() in ("text/html"):
            html_body += part.get_payload()
        if part.get_content_type() in ("text/plain"):
            text_body += part.get_payload(decode=True)
    pre = None
    if html_body != "":
        html_body = email.utils.quote(html_body)
        html_body = re.sub('(\n|\r|\r\n)', '\\\\n', html_body)
        body = Template(decode(html_body)).render({})
    else:
        text_body = email.utils.quote(text_body)
        text_body = re.sub('(\n|\r|\r\n)', '\\\\n', text_body)
        body = decode(text_body)
        pre = True

    return _render(request, 'amavis_quarantine/viewmail.html', {
            "headers" : msg, "alert" : msg.get("X-Amavis-Alert"),
            "body" : body, "pre" : pre, "mail_id" : mail_id,
            "page_id" : request.GET["page"]
            })

@login_required
def viewheaders(request, mail_id):
    conn = db.getconnection("amavis_quarantine")
    status, cursor = db.execute(conn, """
SELECT mail_text
FROM quarantine
WHERE quarantine.mail_id='%s'
""" % mail_id)
    content = ""
    for part in cursor.fetchall():
        content += part[0]
    msg = email.message_from_string(content)
    return _render(request, 'amavis_quarantine/viewheader.html', {
            "headers" : msg.items()
            })

def _redirect_to_index(request, message, count):
    request.user.message_set.create(message=message)
    if count > 1:
        return
    page = request.GET.has_key("page") and request.GET["page"] or "1"
    return HttpResponseRedirect(reverse(index) + "?page=%s" % page)

@login_required
def delete(request, mail_id, count=1):
    conn = db.getconnection("amavis_quarantine")
    if count == 1:
        mail_id = "'%s'" % mail_id
    status, error = db.execute(conn, 
                               "DELETE FROM msgs WHERE mail_id IN (%s)" % mail_id)
    if status:
        message = _("%d message%s deleted successfully" \
                        % (count, count > 1 and "s" or ""))
    else:
        message = error
    return _redirect_to_index(request, message, count)

@login_required
def release(request, mail_id, count=1):
    conn = db.getconnection("amavis_quarantine")
    if count == 1:
        mail_id = "'%s'" % mail_id
    status, cursor = db.execute(conn, """
SELECT msgs.mail_id,secret_id,quar_type,maddr.email FROM msgs, maddr, msgrcpt
WHERE msgrcpt.mail_id=msgs.mail_id AND msgrcpt.rid=maddr.id AND msgs.mail_id IN (%s)
""" % (mail_id))
    emails = {}
    for row in cursor.fetchall():
        if not emails.has_key(row[0]):
            emails[row[0]] = {}
            emails[row[0]]["rcpts"] = []
        emails[row[0]]["secret"] = row[1]
        emails[row[0]]["rcpts"] += [row[3]]
    count = 0
    error = None
    for k, values in emails.iteritems():
        cmd = "/tmp/amavisd-release "
        cmd += "%s %s" % (k, values["secret"])
        for rcpt in values["rcpts"]:
            cmd += " %s" % rcpt
        cmd += "\n"
        result = os.system(cmd)
#            if re.search("250 [^\s]+ Ok", result):
        if not result:
            count += 1
        else:
            error = result
            break
    if not error:
        message = _("%d message%s released successfully" \
                        % (count, count > 1 and "s" or ""))
    else:
        message = error
    _redirect_to_index(request, message, count)

@login_required
def process(request):
    ids = ""
    count = len(request.POST.getlist("selection"))
    for id in request.POST.getlist("selection"):
        if ids != "":
            ids += ","
        ids += "'%s'" % id
    if ids == "":
        return HttpResponseRedirect(reverse(index))
    if request.POST.has_key("release"):
        release(request, ids, count)
            
    if request.POST.has_key("delete"):
        delete(request, ids, count)

    try:
        pagenum = int(request.POST.get('pagenum', '1'))
    except ValueError:
        pagenum = 1
    ctx = _ctx_ok(reverse(index) + "?page=%s" % pagenum)
    return HttpResponse(simplejson.dumps(ctx), 
                        mimetype="application/json")