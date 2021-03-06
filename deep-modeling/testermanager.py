#!/usr/bin/env python
# This file is part of Deemon.

# Deemon is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Deemon is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Deemon.  If not, see <http://www.gnu.org/licenses/>.

import sys
import argparse
import json
import urlparse
import sqlite3 as lite
import os
import datetime
import string
import re

from Cookie import SimpleCookie
from urllib import urlencode

from py2neo.database import Graph
from py2neo import watch

import api.dm_types as dm_types
import utils.log as log
from utils.sqlite import *
from shared.config import NEO4J_HOST, NEO4J_PASSWORD, NEO4J_USERNAME
from api.oppat import *

from api.datamodel.core import PTTerminalNode, PTNonTerminalNode, ParseTree
from api.dm_types import ABSHTTPREQ, TRACE_SINGLETON_OP, SQL
from api.multipart import Multipart
import api.sqlnorm as sqlnorm

import api.typeinfalg.typeinference as typeinf

import csv

DEBUG = False

if DEBUG:
    log.LEVEL = log.LEVELS[-1]
    watch("neo4j.bolt")
else:
    log.LEVEL = log.LEVELS[0]

logger = log.getdebuglogger("tester")
sqlite_schema_tgen = os.path.join(os.getcwd(), "../data/DBSchemaCSRFTests.sql")
sqlite_schema_oracle = os.path.join(os.getcwd(), "../data/DBSchemaOracle.sql")


# COMMENT Credits http://stackoverflow.com/questions/20248355/how-to-get-python-to-gracefully-format-none-and-non-existing-fields

class PartialFormatter(string.Formatter):
    def __init__(self, missing='n.a.', bad_fmt='err.'):
        self.missing, self.bad_fmt = missing, bad_fmt

    def get_field(self, field_name, args, kwargs):
        # Handle a key not found
        try:
            val = super(PartialFormatter, self).get_field(field_name, args, kwargs)
            # Python 3, 'super().get_field(field_name, args, kwargs)' works
        except (KeyError, AttributeError):
            val = None, field_name
        return val

    def format_field(self, value, spec):
        # handle an invalid format
        if value is None:
            value = self.missing
        try:
            return super(PartialFormatter, self).format_field(value, spec)
        except ValueError:
            if self.bad_fmt is not None:
                return self.bad_fmt
            else:
                raise


def test_stats(args, graph, logger=None):

    if logger is not None:
        logger.debug("Retrieving Ops")

    query = """MATCH (ae:AbstractEvent)
                       WITH DISTINCT ae
                RETURN ae.projname AS projname,
                       ae.operation AS operation, count(ae) AS ops
                ORDER BY projname, operation"""

    ops = list(graph.run(query))

    if logger is not None:
        logger.debug("Retrieving stchreqs_tr")

    # COMMENT: This query counts state-changing operation using the singleton on a per-trace.
    query = """MATCH (sql:ParseTree)<-[:ABSTRACTS]-(abssql:AbstractParseTree)
                      WITH DISTINCT abssql, sql
                      WITH abssql, count(sql) AS c
               MATCH (ae:AbstractEvent)-[:ABSTRACTS]->(e:Event)<-[:ACCEPTS]-(t:DFAStateTransition), (e)-[:CAUSED]->(:Event)<-[:PARSES]-(:ParseTree)<-[:ABSTRACTS]-(abssql)
                      WITH DISTINCT ae
               RETURN ae.projname AS projname, ae.operation AS operation, count(ae) AS stchreqs_tr
               ORDER BY projname, operation"""

    stchreqs_tr = list(graph.run(query))

    if logger is not None:
        logger.debug("Retrieving stchreqs_tr_ston")

    query = """MATCH (ae:AbstractEvent)-[:ABSTRACTS]->(e:Event)-[:CAUSED]->(xd)<-[PARSES]-(pt)<-[:ABSTRACTS]-(apt:AbstractParseTree) 
                WITH DISTINCT ae.projname AS projname, ae.operation AS operation, e.uuid AS e_uuid, apt, count(e) AS c 
                WHERE c = 1
                WITH DISTINCT projname, operation, apt.message AS hash, max(c) AS n_of_evt_per_ops
                WITH DISTINCT projname, hash, sum(n_of_evt_per_ops) AS n_of_ops
                WHERE n_of_ops=1
                MATCH (ae:AbstractEvent {projname: projname})-[:ABSTRACTS]->(e:Event)<-[:ACCEPTS]-(t:DFAStateTransition), (e)-[:CAUSED]->(:Event)<-[:PARSES]-(:ParseTree)<-[:ABSTRACTS]-(abssql {message:hash})
                WITH DISTINCT abssql, ae
                WITH abssql.message AS hash, collect(ae) AS ops
                WITH DISTINCT head(ops) AS ae
                RETURN ae.projname AS projname, ae.operation AS operation, count(ae) AS stchreqs_tr_ston
                ORDER BY projname, operation"""

    stchreqs_tr_ston = list(graph.run(query))

    max_pchains_su = list(graph.run(query))

    """
    Group by projname and operation
    """
    merge = {}
    for rs in [ops, stchreqs_tr, stchreqs_tr_ston]:
        for e in list(rs):
            d = dict(e)
            proj_k = e["projname"]
            op_k = e["operation"]
            merge.setdefault(proj_k, {}).setdefault(op_k, {}).update(d)

    out = []
    for proj, ops in merge.iteritems():
        for op, el in ops.iteritems():
            out.append(el)

    out = sorted(out, key=lambda e: e["operation"])
    out = sorted(out, key=lambda e: e["projname"])

    fmt = PartialFormatter()

    col_names = ("PROJECT", "OPERATION", "OPs", "SC OPs", "SC STon OPs")
    hdr = "| {:^24} | {:^60} | {:^14} | {:^14} | {:^14}".format(*col_names)
    print hdr
    print "=" * len(hdr)
    for s in out:
        print fmt.format("| {projname:<24} | {operation:<60} | {ops:>14} | {stchreqs_tr:>14} | {stchreqs_tr_ston:>14}", **s)
    print "\r\n\r\n"


def skip(n, ignore):
    if ignore is not None and ignore == n:
        logger.warning("In message concretization, skipping {}={}".format(n.s_type, n.symbol))
        return True
    return False


def pt_to_url(pt, ignore=None, replacewith=None):
    scheme, netloc, path, qs = "", "", "", ""

    for child in sorted(list(pt.HasChild), key=lambda c: c.pos):

        if isinstance(child, PTTerminalNode):

            if child.s_type == "path":
                path = child.symbol

            elif child.s_type == "scheme":
                scheme = child.symbol

            elif child.s_type == "netloc":
                netloc = child.symbol

            else:
                raise Exception("Unhandled URL component {} {}".format(child.uuid, child.s_type))

        if isinstance(child, PTNonTerminalNode):

            if child.s_type == "query-string":
                qs = pt_to_query(child, ignore, replacewith=replacewith)

            else:
                raise Exception("Unhandled NonTerminal component {} {}".format(child.uuid, child.s_type))

    return urlparse.urlunparse((scheme, netloc, path, "", qs, ""))


def pt_to_query(pt, ignore=None, replacewith=None):
    qs = {}
    parname = None
    for child in sorted(list(pt.HasChild), key=lambda c: c.pos):
        if child.s_type == "param-name":

            if skip(child, ignore):

                parname = None

                if replacewith:
                    parname = replacewith.value

            elif parname is None:
                parname = child.symbol

            else:
                qs.setdefault(parname, []).append("")
                parname = None

        elif child.s_type == "param-value":

            if skip(child, ignore):

                parname = None

                if replacewith:
                    parname = replacewith.value

            elif parname is None:
                logger.warning("Ooops. Two param-values in a row? {} {}".format(child.s_type, child.symbol))

            else:
                qs.setdefault(parname, []).append(child.symbol)
                parname = None
        else:
            Exception("Mmmh... neither param-name nor param-value. This query string is really messed up: {}".format(child.s_type))
    return urlencode(qs, True)


def pt_to_urlformenc(pt, ignore=None, replacewith=None):
    qs = {}
    for child in pt.HasChild:
        name, value = sorted(list(child.HasChild), key=lambda c: c.pos)

        if skip(value, ignore):
            qs.setdefault(name.symbol, [])

            if replacewith:
                qs[name.symbol].append(replacewith.value)

        else:
            qs.setdefault(name.symbol, []).append(unicode(value.symbol).encode('utf-8'))
    return urlencode(qs, True)


def inline_cookie(cookie):
    """Return an inline cookie string"""
    result = []
    items = cookie.items()
    items.sort()
    for K, V in items:
        result.append(V.OutputString())
    return "; ".join(result)


def pt_to_cookie(pt, ignore=None, replacewith=None):
    cookie = SimpleCookie()
    for child in pt.HasChild:
        name, value = sorted(list(child.HasChild), key=lambda c: c.pos)
        if skip(name, ignore):

            if replacewith:
                cookie[str(replacewith.value)] = value

        else:

            if skip(value, ignore):

                if replacewith:
                    cookie[str(replacewith.value)] = value

            else:
                cookie[str(name.symbol)] = str(value.symbol)

    return inline_cookie(cookie)


def pt_to_json(pt, ignore=None, replacewith=None):
    return json.dumps(visit_pt_json(pt, ignore=ignore, replacewith=None))


def visit_pt_json(pt, ignore=None, replacewith=None):
    if isinstance(pt, PTNonTerminalNode):
        if pt.s_type == "json-object":
            obj = dict()
            for child in pt.HasChild:
                k = pt_to_json(child, ignore=ignore, replacewith=replacewith)
                v = pt_to_json(child, ignore=ignore, replacewith=replacewith)
                obj[k] = v
            return obj
        elif pt.s_type == "json-array":
            arr = [pt_to_json(child, ignore) for child in pt.HasChild]
            return arr
    elif isinstance(pt, PTTerminalNode):
        if pt.s_type == "json-string":
            if skip(pt, ignore):
                if replacewith:
                    return str(replacewith.value)
            else:
                return str(pt.symbol)
        elif pt.s_type == "json-number-int":
            if skip(pt, ignore):
                if replacewith:
                    return int(replacewith.value)
            else:
                return int(pt.symbol)
        elif pt.s_type == "json-number-real":
            if skip(pt, ignore):
                if replacewith:
                    return float(replacewith.value)
            else:
                return float(pt.symbol)
        elif pt.s_type == "json-number-bool":
            if skip(pt, ignore):
                if replacewith:
                    return bool(replacewith.value)
            else:
                return bool(pt.symbol)
        elif pt.s_type == "json-number-null":
            if skip(pt, ignore):
                if replacewith:
                    return str(replacewith.value)
            else:
                return None
    else:
        return list(pt.HasChild)[0]


def pt_to_body(pt, ignore=None, replacewith=None):
    ct, body = "", ""
    if pt.dm_type == dm_types.MULTIPART:
        mp = Multipart()
        for child in pt.HasChild:
            name, value = sorted(list(child.HasChild), key=lambda c: c.pos)
            s_name = name.symbol
            s_value = value.symbol

            if skip(name, ignore):
                if replacewith:
                    s_name = replacewith.value
                else:
                    continue
            if skip(value, ignore):
                if replacewith:
                    s_value = replacewith.value
                else:
                    s_value = ""

            mp.field(s_name, s_value)
        ct, body = mp.get()
    elif pt.dm_type == dm_types.JSON:
        ct = ""
        body = pt_to_json(pt, ignore=ignore, replacewith=replacewith)
    elif pt.dm_type == dm_types.FORMURLENC:
        ct = ""
        body = pt_to_urlformenc(pt, ignore=ignore, replacewith=replacewith)
    else:
        ct = ""
        body = pt.message
    return ct, body


def pt_to_req(pt, ignore=None, replacewith=None):
    command = ""
    url = ""
    headers = {}
    ct = ""
    body = None

    Q = list(pt.HasChild)
    while len(Q) > 0:
        child = Q.pop()

        if isinstance(child, PTNonTerminalNode):
            # COMMENT: Special case: we just pick name and value
            if child.s_type == "header-field":
                name, value = sorted(list(child.HasChild), key=lambda c: c.pos)

                k, v = "", ""
                # COMMENT: Adjustment because we screwed up with the pos for URLs
                if isinstance(name, ParseTree):
                    name, value = value, name

                if isinstance(value, ParseTree):

                    if value.dm_type == dm_types.URL:
                        v = pt_to_url(value, ignore=ignore,
                                      replacewith=replacewith)

                    elif value.dm_type == dm_types.COOKIE:
                        v = pt_to_cookie(value, ignore=ignore,
                                         replacewith=replacewith)
                    else:
                        raise Exception("Unhandled ParseTree {} {}".format(value.uuid, value.dm_type))

                else:
                    if skip(value, ignore):
                        if replacewith:
                            v = replacewith.value
                    else:
                        v = value.symbol

                k = name.symbol

                headers.setdefault(k, "")
                if isinstance(v, list):
                    headers[k] = ", ".join(v)
                else:
                    headers[k] = v
            else:
                Q.extend(child.HasChild)

        elif isinstance(child, PTTerminalNode):

            if child.s_type == "method":
                command = child.symbol

            else:
                raise Exception("Unhandled s_type {} {}".format(child.uuid,
                                                                child.symbol))

        elif isinstance(child, ParseTree):

            if child.dm_type == dm_types.URL:
                url = pt_to_url(child, ignore=ignore, replacewith=replacewith)

            elif child.dm_type in dm_types._BODY:
                ct, body = pt_to_body(child, ignore=ignore,
                                      replacewith=replacewith)
            else:
                raise Exception("Unhandled ParseTree {} {}".format(child.uuid,
                                                                   child.dm_type))

        else:
            raise Exception("Unhandled Situation {} {}".format(child.uuid,
                                                               child.dm_type))
    # COMMENT: we have a content type coming from body functions, we need to remove existing ones and replace
    if len(ct) > 0:
        if "content-type" in headers:
            headers["content-type"] = ct

    return command, url, headers, body


def sqlitedb_init(filename, sqlite_schema):
    # COMMENT: If the DB does not exist, lite.connect does not create a folder.
    # COMMENT: Check folder first...
    dirname = os.path.dirname(filename)
    if len(dirname) > 0 and not os.path.exists(dirname):
        logger.info("Folder {0} does not exist. Creating...".format(dirname))
        os.makedirs(dirname)

    if not os.path.exists(filename):
        logger.info("SQLite DB file {0} does not exist. Creating from {1}".format(filename,
                                                                                  sqlite_schema))

        f = open(sqlite_schema)
        con = lite.connect(filename)
        with con:
            cur = con.cursor()
            with f:
                schema = f.read()
                cur.executescript(schema)
        logger.info("SQLite DB file {0} created.".format(filename))


def store_tgen(seq_id, projname, session, operation, user,
               uuid_request, uuid_tn, uuid_src_var,
               uuid_sink_var, method, url, headers, body, dbname):
    headers = json.dumps(headers)

    con = lite.connect(dbname)
    con.text_factory = str
    with con:
        cur = con.cursor()
        # COMMENT: inserting the http_request that triggered the sql_queries
        data = (seq_id, datetime.datetime.now(), projname, session,
                operation, user, uuid_request, uuid_tn, uuid_src_var,
                uuid_sink_var, method, url, headers, body)
        cur.execute("""INSERT INTO CSRF_tests (seq_id,
                                               time,
                                               projname,
                                               session,
                                               operation,
                                               user,
                                               uuid_request,
                                               uuid_tn,
                                               uuid_src_var,
                                               uuid_sink_var,
                                               method,
                                               url,
                                               headers,
                                               body)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    data)
        req_id = cur.lastrowid

    return req_id


_TGEN_RE_VAR_NAME_BLACKLIST = [".*cookie-pair.*", ".*multipart.*", ".*param-name"]
TGEN_RE_VAR_NAME_BLACKLIST = [re.compile(r) for r in _TGEN_RE_VAR_NAME_BLACKLIST]


def _is_var_blacklisted(var_name):
    for p in TGEN_RE_VAR_NAME_BLACKLIST:
        if p.match(var_name) is not None:
            return True
    return False


def tgen_pchain_su_p(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    data = {"len": args.len,
            "projname": args.projname,
            "session": args.session}
    uuids = graph.run("""MATCH acc=(sym:DFAStateTransition {projname:{projname}})-[a:ACCEPTS]->(e1:Event {dm_type:"HttpRequest", session:{session}}), 
                                df=(e2:Event)<-[:BELONGS_TO]-(v2:Variable)<-[:PROPAGATES_TO]-(v1:Variable)-[:BELONGS_TO]->(e1),
                                pt=(p1:ParseTree)-[:PARSES]->(e1), injpt=(p1)-[:HAS_CHILD*]->(tn1:PTTerminalNode)<-[:HAS_VALUE]-(v1)
                         WHERE size(v1.value) > {len}
                 WITH DISTINCT sym, e1, p1, v1, v2, collect(v2) AS dests, tn1
                        RETURN sym.uuid, e1.uuid, e1.dm_type, p1.uuid, v1.uuid, v2.uuid, dests, tn1.uuid
                      ORDER BY e1.symbol""", data)
    uuids = list(uuids)
    print "Total number of test cases to generate: {}".format(len(uuids))
    i = 1
    for res in uuids:

        pt = ParseTree.select(graph).where(uuid=res["p1.uuid"]).first()
        tn = PTTerminalNode.select(graph).where(uuid=res["tn1.uuid"]).first()

        logger.debug( "Exporting test case {}/{} by removing {}={}".format(i, len(uuids), tn.s_type, tn.symbol))        

        command, url, headers, body = pt_to_req(pt, tn)
        if not args.simulate:
            store_tgen(i, res["e1.uuid"], res["tn1.uuid"],
                       res["v1.uuid"], res["v2.uuid"],
                       command, url, headers, body, args.database)
        else:
            print i, res["e1.uuid"], res["tn1.uuid"], res["v1.uuid"], res["v2.uuid"], url

        i += 1


def tgen_su_var(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    query = """MATCH  abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event), 
                     stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                       df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
               WHERE "session_unique" IN v.semtype
                WITH DISTINCT ae,
                              v.name AS var_name,
                              collect([pt, tn, v, e]) AS candidates
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(candidates)[0] AS pt,
                     head(candidates)[1] AS tn,
                     head(candidates)[2] AS v,
                     head(candidates)[3].session AS session,
                     head(candidates)[3].user AS user"""

    data = {
            "projname": args.projname,
            "operation": args.operation,
            "dm_type": ABSHTTPREQ
            }

    uuids = graph.run(query, data)
    uuids = list(uuids)
    print "Total number of test cases to generate: {}".format(len(uuids))
    for i, res in enumerate(uuids):
        if _is_var_blacklisted(res["v"]["name"]):
            logger.debug("Skipping {} because is blacklisted".format(res["v"]["name"]))
            continue
        pt = ParseTree.select(graph).where(uuid=res["pt"]["uuid"]).first()
        tn = PTTerminalNode.select(graph).where(uuid=res["tn"]["uuid"]).first()

        logger.debug("Exporting test case {}/{} by removing {}={}".format(i, len(uuids),
                                                                          tn.s_type, tn.symbol))

        command, url, headers, body = pt_to_req(pt, tn)
        if not args.simulate:
            store_tgen(i, res["projname"], res["session"], res["operation"],
                       res["user"], res["ae"]["uuid"], res["tn"]["uuid"],
                       res["v"]["uuid"], "unknown", command, url,
                       headers, body, args.database)
        else:
            print i, res["projname"], res["operation"], res["v"]["name"], res["v"]["value"],  url          
        i += 1


def _get_singleton_ops(graph, evt_uuid, projname, session, user, logger):
    query = """MATCH (http:Event {dm_type:"HttpRequest", uuid:{evt_uuid}})-[:CAUSED]->(xdebug:Event)<-[:PARSES]->(:ParseTree)<-[:ABSTRACTS]-(apt:AbstractParseTree)
              RETURN DISTINCT apt.uuid AS apt_uuid, apt;"""
    data = {
        "evt_uuid": evt_uuid
    }

    rs = graph.run(query, data)
    rs = list(rs)

    out = []
    for r in rs:
        apt_uuid = r["apt_uuid"]
        label = infer_trace_patterns(graph, apt_uuid, projname,
                                     session, user, logger)
        logger.debug("   {} has abstract query {} with label {}".format(evt_uuid, apt_uuid, label))
        if label == TRACE_SINGLETON_OP:
            out.append(r["apt"])

    return out


def _has_singleton_op(graph, evt_uuid, projname, session, user, logger):
    if len(_get_singleton_ops(graph, evt_uuid, projname, session, user, logger)) > 0:
        return True
    return False


def tgen_su_uu_var_singleton(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    su_query = """MATCH  abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event), 
                     stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                       df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
               WHERE "session_unique" IN v.semtype AND (v.proptype IS null OR NOT ('UG' IN v.proptype))
                WITH DISTINCT ae,
                              v.name AS var_name,
                              collect([pt, tn, v, e]) AS candidates
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(candidates)[0] AS pt,
                     head(candidates)[1] AS tn,
                     head(candidates)[2] AS v,
                     head(candidates)[3].session AS session,
                     head(candidates)[3].user AS user,
                     head(candidates)[3].uuid AS e_uuid"""

    uu_query = """MATCH  abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event), 
                     stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                       df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
               WHERE "user_unique" IN v.semtype AND (v.proptype IS null OR NOT ('UG' IN v.proptype))
                WITH DISTINCT ae,
                              v.name AS var_name,
                              collect([pt, tn, v, e]) AS candidates
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(candidates)[0] AS pt,
                     head(candidates)[1] AS tn,
                     head(candidates)[2] AS v,
                     head(candidates)[3].session AS session,
                     head(candidates)[3].user AS user,
                     head(candidates)[3].uuid AS e_uuid"""

    data = {
            "projname": args.projname,
            "operation": args.operation,
            "dm_type": ABSHTTPREQ
            }

    su_uuids = graph.run(su_query, data)
    su_uuids = list(su_uuids)
    logger.debug("Max number of SU test cases to generate: {}".format(len(su_uuids)))

    uu_uuids = graph.run(uu_query, data)
    uu_uuids = list(uu_uuids)
    logger.debug("Max number of UU test cases to generate: {}".format(len(uu_uuids)))
    
    for label, uuids in [(typeinf.SEM_TYPE_SESSION_UNIQUE, su_uuids), (typeinf.SEM_TYPE_USER_UNIQUE, uu_uuids)]:
        logger.debug("Generating tests for {} variables".format(label))
        for i, res in enumerate(uuids):
            pt = ParseTree.select(graph).where(uuid=res["pt"]["uuid"]).first()
            tn = PTTerminalNode.select(graph).where(uuid=res["tn"]["uuid"]).first()
            command, url, headers, body = pt_to_req(pt, tn)

            logger.debug("Variable {} is {}".format(res["v"]["name"], label))
            logger.debug("  Request {} {}".format(command, url))
            if not _has_singleton_op(graph, res["e_uuid"], res["projname"], res["session"], res["user"], logger):
                logger.debug(" Skipping because does not result in a SINGLETON operation")
                continue

            if _is_var_blacklisted(res["v"]["name"]):
                logger.info(" Skipping because is blacklisted")
                continue

            logger.info( " => Exporting test case {}/{} by removing {}={}".format(i, len(uuids), tn.s_type, tn.symbol))        

            if not args.simulate:
                store_tgen(i, res["projname"], res["session"],
                           res["operation"], res["user"], res["ae"]["uuid"],
                           res["tn"]["uuid"], res["v"]["uuid"], "unknown",
                           command, url, headers, body, args.database)
            else:
                print i, label, res["projname"], res["operation"], res["v"]["name"], res["v"]["value"],  url          


def tgen_su_uu_var_singleton_new_all(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    su_query = """MATCH (ae:AbstractEvent)-[:ABSTRACTS]->(e:Event)-[:CAUSED]->(xd)<-[PARSES]-(pt)<-[:ABSTRACTS]-(apt:AbstractParseTree)
                WITH DISTINCT ae.projname AS projname, ae.operation AS operation, e.uuid AS e_uuid, apt, count(e) AS c
                WHERE c = 1
                WITH DISTINCT projname, operation, apt.message AS hash, max(c) AS n_of_evt_per_ops
                WITH DISTINCT projname, hash, sum(n_of_evt_per_ops) AS n_of_ops
                WHERE n_of_ops=1
                MATCH (ae:AbstractEvent {projname: projname})-[:ABSTRACTS]->(e:Event)<-[:ACCEPTS]-(t:DFAStateTransition), (e)-[:CAUSED]->(:Event)<-[:PARSES]-(:ParseTree)<-[:ABSTRACTS]-(abssql {message:hash})
                WITH DISTINCT abssql, ae
                WITH abssql.message AS hash, collect(ae) AS ops
                WITH DISTINCT head(ops) AS ae

                MATCH (ae)-[:ABSTRACTS]->(e:Event), (pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)

                WHERE ("session_unique" IN v.semtype)
                          AND (v.proptype IS null OR NOT ('UG' IN v.proptype))
                          AND (NOT v.name =~ ".*cookie-pair.*")
                          AND (NOT v.name =~ ".*multipart.*")
                          AND (NOT v.name =~ ".*param-name.*")
                WITH DISTINCT ae,
                              v.name AS var_name,
                              collect([pt, tn, v, e]) AS candidates
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(candidates)[0] AS pt,
                     head(candidates)[1] AS tn,
                     head(candidates)[2] AS v,
                     head(candidates)[3].session AS session,
                     head(candidates)[3].user AS user,
                     head(candidates)[3].uuid AS e_uuid"""

    uu_query = """MATCH (ae:AbstractEvent)-[:ABSTRACTS]->(e:Event)-[:CAUSED]->(xd)<-[PARSES]-(pt)<-[:ABSTRACTS]-(apt:AbstractParseTree)
                WITH DISTINCT ae.projname AS projname, ae.operation AS operation, e.uuid AS e_uuid, apt, count(e) AS c
                WHERE c = 1
                WITH DISTINCT projname, operation, apt.message AS hash, max(c) AS n_of_evt_per_ops
                WITH DISTINCT projname, hash, sum(n_of_evt_per_ops) AS n_of_ops
                WHERE n_of_ops=1
                MATCH (ae:AbstractEvent {projname: projname})-[:ABSTRACTS]->(e:Event)<-[:ACCEPTS]-(t:DFAStateTransition), (e)-[:CAUSED]->(:Event)<-[:PARSES]-(:ParseTree)<-[:ABSTRACTS]-(abssql {message:hash})
                WITH DISTINCT abssql, ae
                WITH abssql.message AS hash, collect(ae) AS ops
                WITH DISTINCT head(ops) AS ae

                MATCH (ae)-[:ABSTRACTS]->(e:Event), (pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
               WHERE ("user_unique" IN v.semtype )
                          AND (v.proptype IS null OR NOT ('UG' IN v.proptype))
                          AND (NOT v.name =~ ".*cookie-pair.*")
                          AND (NOT v.name =~ ".*multipart.*")
                          AND (NOT v.name =~ ".*param-name.*")
                WITH DISTINCT ae,
                              v.name AS var_name,
                              collect([pt, tn, v, e]) AS candidates
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(candidates)[0] AS pt,
                     head(candidates)[1] AS tn,
                     head(candidates)[2] AS v,
                     head(candidates)[3].session AS session,
                     head(candidates)[3].user AS user,
                     head(candidates)[3].uuid AS e_uuid"""

    su_uuids = graph.run(su_query)
    su_uuids = list(su_uuids)
    logger.debug("Max number of SU test cases to generate: {}".format(len(su_uuids)))

    uu_uuids = graph.run(uu_query)
    uu_uuids = list(uu_uuids)
    logger.debug("Max number of UU test cases to generate: {}".format(len(uu_uuids)))

    for label, uuids in [(typeinf.SEM_TYPE_SESSION_UNIQUE, su_uuids), (typeinf.SEM_TYPE_USER_UNIQUE, uu_uuids)]:
        logger.debug("Generating tests for {} variables".format(label))
        for i, res in enumerate(uuids):
            pt = ParseTree.select(graph).where(uuid=res["pt"]["uuid"]).first()
            tn = PTTerminalNode.select(graph).where(uuid=res["tn"]["uuid"]).first()
            command, url, headers, body = pt_to_req(pt, tn)

            logger.debug("Variable {} is {}".format(res["v"]["name"], label))
            logger.debug("  Request {} {}".format(command, url))

            if _is_var_blacklisted(res["v"]["name"]):
                logger.debug(" Skipping because is blacklisted")
                continue

            logger.info( " => Exporting test case {}/{} by removing {}={}".format(i, len(uuids), tn.s_type, tn.symbol))

            if not args.simulate:
                store_tgen(i, res["projname"], res["session"],
                           res["operation"], res["user"],
                           res["ae"]["uuid"], res["tn"]["uuid"],
                           res["v"]["uuid"], "unknown", command, url,
                           headers, body, args.database)
            else:
                row = [i, label, res["projname"], res["operation"],
                       res["v"]["name"], res["v"]["value"],
                       url, res["ae"]["uuid"]]
                csv.writer(sys.stdout,
                           delimiter=",", quotechar="\"").writerow(row)


def tgen_replay_su_uu_var_singleton(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    """
    New query for Session unique (it needs to be adjusted for user unique)
    MATCH abs=(ae:AbstractEvent {projname:"abantecart", operation:"login_and_change_email", dm_type:"AbsHttpRequest"})-[:ABSTRACTS]->(e:Event), stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree), df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode) WHERE "session_unique" IN v.semtype AND (v.proptype IS null OR NOT ('UG' IN v.proptype)) WITH DISTINCT ae, e, pt, tn, v WITH ae, v.name AS var_name, v.user AS user, collect([v]) AS s_vars WITH ae, var_name, collect([user, s_vars]) AS candidates RETURN ae, var_name, head(candidates)[0], length(head(candidates)[1]);
    """

    """
    This query finds all session unique variables protecting a state changing operation.
    It selects also another variable (from the second user session) to be used as
    replacement of the first one.
    """
    su_query = """MATCH abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event), 
                        stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                        df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
                  WHERE "session_unique" IN v.semtype AND (v.proptype IS null OR NOT ('UG' IN v.proptype)) 
                   WITH DISTINCT ae, e, pt, tn, v
                   WITH ae,
                        v.name                     AS var_name,
                        v.user                     AS user,
                        collect([e, pt, tn, v])    AS test_data
                   WITH ae,
                        var_name,
                        collect([user, test_data]) AS candidates
                 RETURN ae,
                        var_name,
                        head(candidates)[0]        AS user,
                        head(candidates)[1][0][0]  AS e,
                        head(candidates)[1][0][1]  AS pt,
                        head(candidates)[1][0][2]  AS tn,
                        head(candidates)[1][0][3]  AS var,
                        head(candidates)[1][1][3]  AS replacement;"""

    """
    Wrt the previous query, this one here group variables by session, not user.
    This because we want to have a replacement for a UU value
    """
    uu_query = """MATCH abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event), 
                        stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                        df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
                  WHERE "user_unique" IN v.semtype AND (v.proptype IS null OR NOT ('UG' IN v.proptype))
                   WITH DISTINCT ae, e, pt, tn, v
                   WITH ae,
                        v.name                        AS var_name,
                        v.session                     AS session,
                        collect([e, pt, tn, v])       AS test_data
                   WITH ae,
                        var_name,
                        collect([session, test_data]) AS candidates
                 RETURN ae,
                        var_name,
                        head(candidates)[0]           AS session,
                        head(candidates)[1][0][0]     AS e,
                        head(candidates)[1][0][1]     AS pt,
                        head(candidates)[1][0][2]     AS tn,
                        head(candidates)[1][0][3]     AS var,
                        head(candidates)[1][1][3]     AS replacement"""

    data = {
            "projname": args.projname,
            "operation": args.operation,
            "dm_type": ABSHTTPREQ
            }

    su_uuids = graph.run(su_query, data)
    su_uuids = list(su_uuids)
    logger.debug("Max number of SU test cases to generate: {}".format(len(su_uuids)))

    uu_uuids = graph.run(uu_query, data)
    uu_uuids = list(uu_uuids)
    logger.debug("Max number of UU test cases to generate: {}".format(len(uu_uuids)))
    
    for label, uuids in [(typeinf.SEM_TYPE_SESSION_UNIQUE, su_uuids), (typeinf.SEM_TYPE_USER_UNIQUE, uu_uuids)]:
        logger.info("Generating tests for {} variables".format(label))
        for i, res in enumerate(uuids):
            pt = ParseTree.select(graph).where(uuid=res["pt"]["uuid"]).first()
            tn = PTTerminalNode.select(graph).where(uuid=res["tn"]["uuid"]).first()
            command, url, headers, body = pt_to_req(pt, tn)

            logger.info("Variable {} is {}".format(res["v"]["name"], label))
            logger.info("  Request {} {}".format(command, url))
            if not _has_singleton_op(graph, res["e_uuid"], res["projname"], res["session"], res["user"], logger):
                logger.debug(" Skipping because does not result in a SINGLETON operation")
                continue

            if _is_var_blacklisted(res["v"]["name"]):
                logger.debug(" Skipping because is blacklisted")
                continue

            logger.info(" => Exporting test case {}/{} by removing {}={}".format(i, len(uuids), tn.s_type, tn.symbol))        

            if not args.simulate:
                store_tgen(i, res["projname"], res["session"],
                           res["operation"], res["user"], res["ae"]["uuid"],
                           res["tn"]["uuid"], res["v"]["uuid"], "unknown",
                           command, url, headers, body, args.database)
            else:
                print i, label, res["projname"], res["operation"], res["v"]["name"], res["v"]["value"],  url          


def tgen_not_protected_all_new(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    query = """MATCH (ae:AbstractEvent)-[:ABSTRACTS]->(e:Event)-[:CAUSED]->(xd)<-[PARSES]-(pt)<-[:ABSTRACTS]-(apt:AbstractParseTree) 
                WITH DISTINCT ae.projname AS projname, ae.operation AS operation, e.uuid AS e_uuid, apt, count(e) AS c 
                WHERE c = 1
                WITH DISTINCT projname, operation, apt.message AS hash, max(c) AS n_of_evt_per_ops
                WITH DISTINCT projname, hash, sum(n_of_evt_per_ops) AS n_of_ops
                WHERE n_of_ops=1
                MATCH (ae:AbstractEvent {projname: projname})-[:ABSTRACTS]->(e:Event)<-[:ACCEPTS]-(t:DFAStateTransition), (e)-[:CAUSED]->(:Event)<-[:PARSES]-(:ParseTree)<-[:ABSTRACTS]-(abssql {message:hash})
                WITH DISTINCT abssql, ae
                WITH abssql.message AS hash, collect(ae) AS ops
                WITH DISTINCT head(ops) AS ae
                MATCH (ae)-[:ABSTRACTS]->(e:Event), (pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
                    WITH DISTINCT  ae, e, collect([pt,tn,v]) AS vars
                    WITH DISTINCT  ae, collect([e, vars]) AS e_vars
               RETURN ae,
                      ae.projname AS projname,
                      ae.operation AS operation,
                      head(e_vars)[0] AS e,
                      head(e_vars)[1] AS vars"""

    uuids = graph.run(query)
    uuids = list(uuids)
    logger.debug("Number of requests to process: {}".format(len(uuids)))

    def _is_not_protected(res):
        V = res["vars"]
        for pt, tn, v in V:
            if not set([str(typeinf.SEM_TYPE_SESSION_UNIQUE), str(typeinf.SEM_TYPE_USER_UNIQUE)]).isdisjoint(set(v.get("semtype", []))):
                """
                This request has a UU/UG variable. We still don't know if this is a blacklisted one, e.g., session cookie.
                """
                if not _is_var_blacklisted(v["name"]):
                    """
                    The variable is not blacklisted => PROTECTED
                    """
                    return False

        return True

    not_protected = filter(_is_not_protected, uuids)
    logger.info("No. of NON protected state changing operations: {}".format(len(not_protected)))

    for i, res in enumerate(not_protected):
        pt, _, __ = res["vars"][0]
        pt = ParseTree.select(graph).where(uuid=pt["uuid"]).first()
        command, url, headers, body = pt_to_req(pt)

        if not args.simulate:
            store_tgen(i, res["projname"], res["e"]["session"],
                       res["operation"], res["e"]["user"],
                       res["ae"]["uuid"], "unknown", "unknown",
                       "unknown", command, url, headers, body,
                       args.database)
        else:
            print "{},{},{},{},{}".format(i, res["projname"],
                                          res["operation"], url,
                                          res["ae"]["uuid"])


def tgen_not_protected(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    query = """MATCH abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event),
                     stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                       df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
       WITH DISTINCT ae,
                     e,
                     collect([pt, tn, v]) AS vars
       WITH DISTINCT ae,
                     collect([e, vars]) AS e_vars
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(e_vars)[0] AS e,
                     head(e_vars)[1] AS vars"""

    data = {
            "projname": args.projname,
            "operation": args.operation,
            "dm_type": ABSHTTPREQ
            }

    uuids = graph.run(query, data)
    uuids = list(uuids)
    logger.debug("Number of requests to process: {}".format(len(uuids)))

    def _is_not_protected(res):
        V = res["vars"]
        for pt, tn, v in V:
            if not set([str(typeinf.SEM_TYPE_SESSION_UNIQUE), str(typeinf.SEM_TYPE_USER_UNIQUE)]).isdisjoint(set(v.get("semtype", []))):
                """
                This request has a UU/UG variable. We still don't know if this is a blacklisted one, e.g., session cookie.
                """
                if not _is_var_blacklisted(v["name"]):
                    """
                    The variable is not blacklisted => PROTECTED
                    """
                    return False

        return True

    st_ch_ops = [res for res in uuids if _has_singleton_op(graph, res["e"]["uuid"], res["projname"], res["e"]["session"], res["e"]["user"], logger)]
    logger.debug("N.ro of state changing operations: {}".format(len(st_ch_ops)))

    not_protected = filter(_is_not_protected, st_ch_ops)
    logger.info("No. of NON protected state changing operations: {}".format(len(not_protected)))

    for i, res in enumerate(not_protected):
        pt, _, __ = res["vars"][0]
        pt = ParseTree.select(graph).where(uuid=pt["uuid"]).first()
        command, url, headers, body = pt_to_req(pt)

        if not args.simulate:
            store_tgen(i, res["projname"], res["e"]["session"],
                       res["operation"], res["e"]["user"],
                       res["ae"]["uuid"], "unknown", "unknown",
                       "unknown", command, url, headers,
                       body, args.database)
        else:
            print i, res["projname"], res["operation"], url


def tgen_protected(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.database, sqlite_schema_tgen)

    query = """MATCH abs=(ae:AbstractEvent {projname:{projname}, operation:{operation}, dm_type:{dm_type}})-[:ABSTRACTS]->(e:Event), 
                     stch=(e)-[:CAUSED]->(m:Event)<-[:PARSES]-(s:ParseTree),
                       df=(pt:ParseTree)-[:PARSES]->(e)<-[:BELONGS_TO]-(v:Variable)-[:HAS_VALUE]->(tn:PTTerminalNode)
       WITH DISTINCT ae,
                     e,
                     collect([pt, tn, v]) AS vars
       WITH DISTINCT ae,
                     collect([e, vars]) AS e_vars
              RETURN ae,
                     ae.projname AS projname,
                     ae.operation AS operation,
                     head(e_vars)[0] AS e,
                     head(e_vars)[1] AS vars"""

    data = {
            "projname": args.projname,
            "operation": args.operation,
            "dm_type": ABSHTTPREQ
            }

    uuids = graph.run(query, data)
    uuids = list(uuids)
    logger.debug("Number of requests to process: {}".format(len(uuids)))
    
    def _is_protected(res):
        V = res["vars"]
        for pt, tn, v in V:
            if not set([str(typeinf.SEM_TYPE_SESSION_UNIQUE), str(typeinf.SEM_TYPE_USER_UNIQUE)]).isdisjoint(set(v.get("semtype", []))):
                """
                This request has a UU/UG variable. We still don't know
                if this is a blacklisted one, e.g., session cookie.
                """
                if not _is_var_blacklisted(v["name"]):
                    """
                    The variable is not blacklisted => PROTECTED
                    """
                    return True

        return False

    st_ch_ops = [res for res in uuids if _has_singleton_op(graph, res["e"]["uuid"], res["projname"], res["e"]["session"], res["e"]["user"], logger)]
    logger.debug("N.ro of state changing operations: {}".format(len(st_ch_ops)))

    protected = filter(_is_protected, st_ch_ops)
    logger.info("No. of protected state changing operations: {}".format(len(protected)))

    for i, res in enumerate(protected):
        pt, _, __ = res["vars"][0]
        pt = ParseTree.select(graph).where(uuid=pt["uuid"]).first()
        command, url, headers, body = pt_to_req(pt)

        if not args.simulate:
            store_tgen(i, res["projname"], res["e"]["session"],
                       res["operation"], res["e"]["user"],
                       res["ae"]["uuid"], "unknown", "unknown",
                       "unknown", command, url, headers, body,
                       args.database)
        else:
            print i, res["projname"], res["operation"], url


def store_oracle_output(seq_id, projname, session, operation, user,
                        uuid_request, uuid_tn, uuid_src_var,
                        uuid_sink_var, method, url, headers,
                        body, query_message, query_hash, apt_uuid,
                        observed, tr_pattern, dbname):
    headers = json.dumps(headers)

    con = lite.connect(dbname)
    con.text_factory = str
    with con:
        cur = con.cursor()
        data = (seq_id, datetime.datetime.now(), projname, session,
                operation, user, uuid_request, uuid_tn, uuid_src_var,
                uuid_sink_var, method, url, headers, body,
                query_message, query_hash, apt_uuid, observed, tr_pattern)
        cur.execute("""INSERT INTO CSRF_tests_results (seq_id,
                                                       time,
                                                       projname,
                                                       session,
                                                       operation,
                                                       user,
                                                       uuid_request,
                                                       uuid_tn,
                                                       uuid_src_var,
                                                       uuid_sink_var,
                                                       method,
                                                       url,
                                                       headers,
                                                       body,
                                                       query_message,
                                                       query_hash,
                                                       apt_uuid,
                                                       observed,
                                                       tr_pattern)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    data)
        _id = cur.lastrowid

    return _id


def _hash(q):
    return sqlnorm.generate_normalized_query_hash(q)


def _sanitize(q):
    if q.startswith("'"):
        q = q[1:-1]
    return q


def _get_abs_query(graph, ae_uuid, tn_uuid, abs_message):
    """
    For each abstract query, we count the number of queries
    """
    cypher = """MATCH abs=(ae:AbstractEvent {uuid:{ae_uuid}})-[:ABSTRACTS]->(e:Event),
                          (e)<-[:PARSES]-(pt:ParseTree)-[:HAS_CHILD*..5]->(tn:PTTerminalNode {uuid:{tn_uuid}}),
                          (e)-[:CAUSED]->(xd:Event)<-[:PARSES]-(q:ParseTree {dm_type:{q_type}}),
                      ptabs=(aq:AbstractParseTree {message:{abs_message}})-[:ABSTRACTS]->(q)
                RETURN aq"""

    data = {
        "ae_uuid": ae_uuid,
        "tn_uuid": tn_uuid,
        "abs_message": abs_message,
        "q_type": SQL
    }

    rs = graph.run(cypher, data)

    return list(rs)


def oracle_stats(args, graph, logger=None):

    sqlitedb_init(args.output, sqlite_schema_oracle)

    csrftests = load_csrftests_sqlite(args.testcases, logger)

    con = lite.connect(args.output)
    con.text_factory = str

    for t in csrftests:
        """
        Queries and abstract queries in the model
        """
        cypher = """MATCH abs=(ae:AbstractEvent {uuid:{ae_uuid}})-[:ABSTRACTS]->(e:Event),
                              (e)<-[:PARSES]-(pt:ParseTree)-[:HAS_CHILD*..5]->(tn:PTTerminalNode {uuid:{tn_uuid}}),
                              (e)-[:CAUSED]->(xd:Event)<-[:PARSES]-(q:ParseTree {dm_type:{q_type}}),
                          ptabs=(aq:AbstractParseTree)-[:ABSTRACTS]->(q)
                    RETURN q, aq"""

        data = {
            "ae_uuid": t[7],
            "tn_uuid": t[8],
            "q_type": SQL,
        }

        queries = graph.run(cypher, data)

        queries = list(queries)
        queries = sorted(queries, key=lambda e: e["q"]["message"])

        Q1 = map(lambda e: e["q"]["message"], queries)
        H1 = map(lambda e: e["aq"]["message"], queries)
        h1 = "-".join(H1)

        """
        Patterns of abstract queries
        """
        def _extend_with_patterns(e):
            p = infer_trace_patterns(graph, e["aq"]["uuid"],
                                     t[3], t[4], t[6], logger)
            return (e["aq"]["message"], (e["aq"], p))

        Pts = dict(map(_extend_with_patterns, queries))

        """
        Queries and abstract observed while testing
        """

        Q2 = load_queries_by_id_sqlite(args.analyzed, t[1], logger)
        # COMMENT: e[2] is the SQL query
        Q2 = sorted(map(lambda e: _sanitize(e[2]), Q2))
        # abstracts
        H2 = [_hash(q) for q in Q2]
        # chain hashes
        h2 = "-".join(H2)

        print ""
        print t[1], t[11], t[12], t[9]
        print "=" * 80
        if h1 == h2:
            print ""
            print ">>> State changes match <<<"
            print ""

        for h, q in zip(H1, Q1):
            print "     H1 = {:100} {}".format(q[:100], h)
        print "      ------------"
        for query_hash, query_message in zip(H2, Q2):
            observed = "OBSERVED" if query_hash in H1 else "NEW"
            
            apt = Pts.get(query_hash, (None, "NOT_FOUND"))[0]
            apt_uuid = None
            if apt:
                apt_uuid = apt["uuid"]

            tr_pattern = Pts.get(query_hash, (None, "NOT_FOUND"))[1]
            print "     H2 = {:100} {} {} {}".format(query_message[:100],
                                                     query_hash,
                                                     tr_pattern, observed)

            store_oracle_output(t[1], t[3], t[4], t[5], t[6], t[7], t[8],
                                t[9], t[10], t[11], t[12], t[13], t[14],
                                query_message, query_hash, apt_uuid,
                                observed, tr_pattern, args.output)


def _get_evtuuid_from_absreq(graph, ae_uuid, projname, session, user, logger):
    query = """MATCH (ae:AbstractEvent {uuid:{ae_uuid}})-[:ABSTRACTS]->(e:Event {dm_type:"HttpRequest", projname:{projname}, session:{session}, user:{user}})
              RETURN DISTINCT e.uuid AS e_uuid;"""
    data = {
        "ae_uuid": ae_uuid,
        "projname": projname,
        "session": session,
        "user": user
    }

    rs = graph.run(query, data)
    rs = list(rs)
    print(rs)
    return rs[0]["e_uuid"]


def oracle_st_chng(args, graph, logger=None):
    if not args.simulate:
        sqlitedb_init(args.output, sqlite_schema_oracle)

    csrftests = load_csrftests_sqlite(args.testcases, logger)

    con = lite.connect(args.output)
    con.text_factory = str

    for t in csrftests:
        """
        Get SINGLETON from t
        """

        e_uuid = _get_evtuuid_from_absreq(graph, t[7], t[3],
                                          t[4], t[6], logger)
        ae_ops = _get_singleton_ops(graph, e_uuid, t[3], t[4], t[6], logger)
        if len(ae_ops) == 0:
            logger.warning("No SINGLETON for {} {} {} {}".format(t[7], t[3],
                                                                 t[4], t[6]))
            continue

        H_model = map(lambda ae: ae["message"], ae_ops)

        """
        Queries and abstract observed while testing
        """
        Q_exec = load_queries_by_id_sqlite(args.analyzed, t[1], logger)
        # COMMENT: e[2] is the SQL query
        Q_exec = sorted(map(lambda e: _sanitize(e[2]), Q_exec))
        H_exec = [_hash(q) for q in Q_exec]

        print ""
        print t[1], t[11], t[12], "H_model", len(H_model)
        print "=" * 80

        for query_hash, query_message in zip(H_exec, Q_exec):
            st_ch = "ST_CHNG" if query_hash in H_model else "NO"
            print "    {} = {:10} {}".format(query_hash, st_ch,
                                             query_message[:40])

            if not args.simulate:
                store_oracle_output(t[1], t[3], t[4], t[5], t[6], t[7],
                                    t[8], t[9], t[10], t[11], t[12],
                                    t[13], t[14], query_message,
                                    query_hash, ae_ops[0]["uuid"],
                                    st_ch, "NONE", args.output)


def oracle_create_reference_query_hash_dic(tc_references,
                                           tc_analyzed_references,
                                           logger=None):
    """
    this whole scheme rests on the assumtion that the user does
    not mix the order of the reference files and the analyzed
    reference files. Will be addressed in future versions.
    """
    references = tc_references.split(",")
    analyzed_references = tc_analyzed_references.split(",")
    assert len(references) == len(analyzed_references)

    reference_dict = {}
    for ref, aref in zip(references, analyzed_references):
        tc_references= load_csrftests_sqlite(ref, logger)        
        for tc_reference in tc_references:
            queries = load_queries_by_id_sqlite(aref, tc_reference[1],
                                                logger)
            for query in queries:
                hash = _hash(_sanitize(query[2]))
                reference_dict[hash] = True

    return reference_dict


def oracle_yes_no(args, graph, logger=None):
    csrftests = load_csrftests_sqlite(args.tc, logger)

    for csrftest in csrftests:
        e_uuid = _get_evtuuid_from_absreq(graph, csrftest[7], csrftest[3],
                                          csrftest[4], csrftest[6], logger)
        ae_ops = _get_singleton_ops(graph, e_uuid, csrftest[3], csrftest[4],
                                    csrftest[6], logger)
        assert len(ae_ops) != 0, "No SINGLETON for {} {} {} {}".format(csrftest[7],
                                                                       csrftest[3],
                                                                       csrftest[4],
                                                                       csrftest[6])
        # COMMENT: contains the abstract parse trees of the Xdebug related queries
        # COMMENT: the message part of the node is the hashed (related) query
        H_model = map(lambda ae: ae["message"], ae_ops)

        """
        Queries and abstract observed while testing
        """
        # COMMENT: load sql queries from analyzed sqlite database
        Q_exec = load_queries_by_id_sqlite(args.tc_analyzed,
                                           csrftest[1],
                                           logger)
        # COMMENT: e[2] is the SQL query
        Q_exec = sorted(map(lambda e: _sanitize(e[2]), Q_exec))
        H_exec = [_hash(q) for q in Q_exec]

        print ""
        print csrftest[1], csrftest[11], csrftest[12], "H_model", len(H_model)
        print "=" * 80

        """
        COMMENT: go through all observed queries in tc_analyzed
                 (limited by analyzer to state changing only)
        """
        summary = []
        reference_dict = oracle_create_reference_query_hash_dic(args.tc_references,
                                                                args.tc_analyzed_references,
                                                                logger)
        for query_hash, query_message in zip(H_exec, Q_exec):
            observed = True if query_hash in H_model else False
            unique = True if query_hash not in reference_dict else False
            print "    {} = {:20} {:20} {}".format(query_hash,
                                                 "observed" if observed else "not observed",
                                                 "unique" if unique else "not unique",
                                                 query_message[:40])
            summary.append([query_hash, query_message, observed, unique])

        hits = [True for qh, qm, ob, uni in summary if ob and uni]
        if len(hits) > 0:
            print ">>> VULNERABILITY DETECTED <<<"
            print ""
        else:
            print ">>> NO VULNERABILITY DETECTED <<<"
            print ""


def parse_args(args):
    p = argparse.ArgumentParser(description='tester parameters')
    p.add_argument("-v", "--verbose",
                   dest="verbose",
                   action="store_true",
                   help="if this flag is set additional debug information is printed")

    subp = p.add_subparsers()

    stats_p = subp.add_parser("stats", help="Get some statistics about the testing phase") 
    stats_p.set_defaults(func=test_stats)

    """
    ===============
    TEST GENERATION
    ===============
    """

    tests_p = subp.add_parser("tgen", help="Test case generator functions")
    tests_subp = tests_p.add_subparsers()

    not_protected_p = tests_subp.add_parser("not_protected",
                                            help="Generate a test for each non protected HTTP requests that lead to a SINGLETON operation")
    not_protected_p.add_argument("projname", help="Project name")
    not_protected_p.add_argument("operation",  help="Operation")
    not_protected_p.add_argument("database",
                                 help="Database where to store HTTP requests")
    not_protected_p.add_argument('--simulate',
                                 help="Do not write to database",
                                 action="store_true")
    not_protected_p.set_defaults(func=tgen_not_protected)


    protected_p = tests_subp.add_parser("protected",
                                        help="Generate a test for each protected HTTP requests that lead to a SINGLETON operation")
    protected_p.add_argument("projname",
                             help="Project name")
    protected_p.add_argument("operation",
                             help="Operation")
    protected_p.add_argument("database",
                             help="Database where to store HTTP requests")
    protected_p.add_argument('--simulate',
                             help="Do not write to database",
                             action="store_true")
    protected_p.set_defaults(func=tgen_protected)

    """
    ===========
    TEST ORACLE
    ===========
    """

    oracle_p = subp.add_parser("oracle",
                               help="Test case oracle to determine vulnerabilities")
    oracle_p.add_argument("tc_references",
                          help="csv list of sqlite databases of test cases for reference")
    oracle_p.add_argument("tc_analyzed_references",
                          help="csv list of rawtrace-analysis sqlite databases of the test cases for reference")
    oracle_p.add_argument("tc",
                          help="database with the test case")
    oracle_p.add_argument("tc_analyzed",
                          help="rawtrace-analysis database of the test case")
    oracle_p.set_defaults(func=oracle_yes_no)

    return p.parse_args(args)


def main(args):
    graph = Graph(host=NEO4J_HOST, user=NEO4J_USERNAME,
                  password=NEO4J_PASSWORD)
    args_obj = parse_args(args)

    if args_obj.verbose:
        logger.setLevel(log.LEVELS[-1])
    else:
        logger.setLevel(log.LEVELS[0])

    args_obj.func(args_obj, graph, logger)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))


"""
Additional indexes:
:DataValue(value)
:KeyValuePair(value)
:SQLToken(value)
"""
