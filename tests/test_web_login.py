"""Tests for the web_login module (HTML parsing and param extraction)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from vnc2ipkvm.web_login import _parse_applet_params, _has_applet_params


SAMPLE_APPLET_HTML = """\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><title>Remote IP Console Title</title></head>
<body>
<applet code="nn.pp.rc.RemoteConsoleApplet.class" archive="rc.jar">
  <param name="APPLET_ID" value="44A269425D75E97DE98221BA5D921A9B3F2DEFCC0D547BB10BD17EC03284BDFC">
  <param name="PORT" value="443">
  <param name="SSLPORT" value="443">
  <param name="PROTOCOL_VERSION" value="01.11">
  <param name="PORT_ID" value="0">
  <param name="NORBOX" value="no">
  <param name="NORBOX_IPV4TARGET" value="">
  <param name="NORBOX_IPV6TARGET" value="">
  <param name="SSL" value="off">
  <param name="KBD_LAYOUT" value="104pc">
  <param name="BOARD_NAME" value="Remote IP Console">
  <param name="SelEnc" value="1">
  <param name="FixEncInd" value="1">
</applet>
</body></html>
"""

SAMPLE_LOGIN_HTML = """\
<!DOCTYPE HTML>
<html><head><title>Remote IP Console Authentication</title></head>
<body>
<form action="auth.asp" method="POST">
<input type="text" name="login" value="">
<input type="password" name="password" value="">
</form>
</body></html>
"""


class TestHasAppletParams(unittest.TestCase):

    def test_applet_page(self):
        self.assertTrue(_has_applet_params(SAMPLE_APPLET_HTML))

    def test_login_page(self):
        self.assertFalse(_has_applet_params(SAMPLE_LOGIN_HTML))

    def test_empty(self):
        self.assertFalse(_has_applet_params(""))


class TestParseAppletParams(unittest.TestCase):

    def test_extracts_applet_id(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["APPLET_ID"],
                         "44A269425D75E97DE98221BA5D921A9B3F2DEFCC0D547BB10BD17EC03284BDFC")

    def test_extracts_port(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["PORT"], "443")

    def test_extracts_protocol_version(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["PROTOCOL_VERSION"], "01.11")

    def test_extracts_ssl(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["SSL"], "off")

    def test_extracts_norbox(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["NORBOX"], "no")

    def test_extracts_port_id(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["PORT_ID"], "0")

    def test_extracts_all_params(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertGreater(len(params), 10)

    def test_param_names_uppercased(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        for key in params:
            self.assertEqual(key, key.upper())

    def test_empty_values_preserved(self):
        params = _parse_applet_params(SAMPLE_APPLET_HTML)
        self.assertEqual(params["NORBOX_IPV4TARGET"], "")

    def test_raises_on_missing_applet_id(self):
        html = '<html><param name="PORT" value="443"></html>'
        with self.assertRaises(ValueError):
            _parse_applet_params(html)

    def test_single_quoted_params(self):
        html = "<param name='APPLET_ID' value='ABCDEF123456'>"
        params = _parse_applet_params(html)
        self.assertEqual(params["APPLET_ID"], "ABCDEF123456")

    def test_mixed_case_tag(self):
        html = '<PARAM NAME="APPLET_ID" VALUE="ABC123">'
        params = _parse_applet_params(html)
        self.assertEqual(params["APPLET_ID"], "ABC123")


if __name__ == "__main__":
    unittest.main()
