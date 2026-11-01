import socket
import unittest
import urllib.error
from unittest.mock import patch

from notion_sync import Notion, StopSync


class TransportTests(unittest.TestCase):
    def test_known_no_connection_vs_ambiguous_timeout(self):
        cases = [(socket.gaierror(11001, 'lookup failed'), 'NOTION_NOT_SENT'),
                 (ConnectionRefusedError('refused'), 'NOTION_NOT_SENT'),
                 (TimeoutError('timeout'), 'NOTION_OUTCOME_UNKNOWN'),
                 (ConnectionResetError('response lost'), 'NOTION_OUTCOME_UNKNOWN')]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), \
                 patch('notion_sync.notion_token', return_value='test-only'), \
                 patch('notion_sync.time.sleep'), \
                 patch('notion_sync.urllib.request.urlopen', side_effect=urllib.error.URLError(error)) as request:
                with self.assertRaisesRegex(StopSync, expected):
                    Notion().request('PATCH', 'blocks/example/children', {'children': []})
                self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
