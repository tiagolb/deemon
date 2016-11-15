"""
DEEP MODEL TYPES
"""

"""
Selenium
"""

SELENESE   = "SeleneseCommand"

"""
HTTP-related
"""

URL        = "URL"
HTTPREQ    = "HttpRequest"
HTTPRESP   = "HttpResponse"
COOKIE     = "Cookie"
SETCOOKIE  = "SetCookie"

"""
Message encoding related
"""
MULTIPART  = "Multipart"
FORMURLENC = "FormURLEncoded"
JSON       = "JSON"
_BODY      = [MULTIPART, FORMURLENC, JSON]

"""
Server-side Program related
"""
XDEBUG     = "Xdebug"
PHPSESSION = "PHPSession"

"""
Disk I/O related
"""
SQL        = "SQLQuery"
ABSQUERY   = "AbsQuery"
                    

"""
PROPAGATION CHAIN TYPES
"""

UG         = "UG"