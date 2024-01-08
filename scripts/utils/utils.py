import gzip
import json
import os
import re
import ssl
import threading
from contextlib import contextmanager
from typing import Optional, Any, Tuple

import mistletoe
import requests

from common import constants, log
from common.report import reporter


def remove_prefix(text: str, prefix: str) -> str:
	pos = text.find(prefix)
	return text[pos + len(prefix):] if pos >= 0 else text


def remove_suffix(text: str, suffix: str) -> str:
	pos = text.rfind(suffix)
	return text[:pos] if pos >= 0 else text


def format_markdown(text: str) -> str:
	for c in ('\\', '<', '>'):
		text = text.replace(c, '\\' + c)
	return text


def load_json(file_path: str) -> dict:
	if os.path.isfile(file_path):
		with open(file_path, encoding='utf8') as file:
			return json.load(file)
	else:
		raise FileNotFoundError('File {} not found when loading json'.format(file_path))


@contextmanager
def read_file(file_path: str):
	"""
	ensure utf8
	"""
	with open(file_path, 'r', encoding='utf8') as file:
		yield file


@contextmanager
def write_file(file_path: str):
	"""
	Just like open() in 'w' mode, but create the directory automatically
	"""
	dir_path = os.path.dirname(file_path)
	if not os.path.isdir(dir_path):
		os.makedirs(dir_path)
	with open(file_path, 'w', encoding='utf8') as file:
		yield file


def save_json(data: dict, file_path: str, *, compact: bool = False, with_gz: bool = False):
	if compact:
		s = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
	else:
		s = json.dumps(data, indent=2, ensure_ascii=False)

	with write_file(file_path) as f:
		f.write(s)
	if with_gz:
		with gzip.GzipFile(file_path + '.gz', 'wb', mtime=0) as zf:
			zf.write(s.encode('utf8'))


def request_get(url: str, *, headers: dict = None, params: dict = None, retries: int = 3) -> requests.Response:
	"""
	requests.get wrapper with retries for connection / ssl errors
	"""
	err = None
	for i in range(max(1, retries)):
		if constants.DEBUG.REQUEST_GET:
			log.debug('\tRequesting {}/{} url={} params={}'.format(i + 1, retries, url, params))
		try:
			return requests.get(url, params=params, proxies=constants.PROXIES, headers=headers)
		except (requests.exceptions.ConnectionError, ssl.SSLError) as e:
			err = e
	if err is not None:
		raise err from None


def request_github_api(url: str, *, params: dict = None, etag: str = '', retries: int = 3) -> Tuple[Optional[Any], str]:
	"""
	Return None if etag doesn't change, in the other word, the response data doesn't change
	"""
	headers = {
		'If-None-Match': etag
	}
	if 'github_api_token' in os.environ:
		headers['Authorization'] = 'token {}'.format(os.environ['github_api_token'])
	response = request_get(url, headers=headers, params=params, retries=retries)
	try:
		new_etag = response.headers['ETag']
	except KeyError:
		log.error('No ETag in response! url={}, params={} status_code={}, content={}'.format(url, params, response.status_code, response.content))
		raise
	remaining, limit = response.headers['X-RateLimit-Remaining'], response.headers['X-RateLimit-Limit']
	reporter.record_rate_limit(remaining, limit)
	if constants.DEBUG.SHOW_RATE_LIMIT:
		log.debug('\tRateLimit: {}/{}'.format(remaining, limit))
		log.debug('\tETag: {} -> {}, url={}, params={}'.format(etag, new_etag, url, params))

	# strange prefix. does not affect accuracy, but will randomly change from time to time
	# so yeets it here in advance
	if new_etag.startswith('W/'):
		new_etag = new_etag[2:]
	if response.status_code == 304:
		return None, new_etag
	if response.status_code != 200:
		raise Exception('Un-expected status code {}: {}'.format(response.status_code, response.content))
	return response.json(), new_etag


def pretty_file_size(size: int) -> str:
	for c in ('B', 'KB', 'MB', 'GB', 'TB'):
		unit = c
		if size < 2 ** 10:
			break
		size /= 2 ** 10
	return str(round(size, 2)) + unit


# https://github.com/miyuchina/mistletoe/issues/210
__rewrite_markdown_lock = threading.Lock()


def rewrite_markdown(content: str, repos_url: str, raw_url: str) -> str:
	from mistletoe.markdown_renderer import MarkdownRenderer
	from mistletoe.span_token import Image, Link

	repos_url = repos_url.rstrip('/')
	raw_url = raw_url.rstrip('/')
	content = content.replace('\r\n', '\n')
	pattern = re.compile(r'^\w+://', re.ASCII)

	def rewrite_url(url: str, rewrite_base: str) -> str:
		if not pattern.match(url):  # relative path
			if url in ['', '.'] or url.startswith('#'):
				pass  # keep untouched
			else:
				new_url = rewrite_base + '/' + url
				log.info('URL rewritten for {!r}: {!r} -> {!r}'.format(repos_url, url, new_url))
				return new_url
		return url

	def rewrite_children(node):
		if isinstance(node, Image):
			node.src = rewrite_url(node.src, raw_url)
		elif isinstance(node, Link):
			node.target = rewrite_url(node.target, repos_url)

		children = getattr(node, 'children', [])
		for child in children:
			rewrite_children(child)

	with __rewrite_markdown_lock:
		with MarkdownRenderer() as renderer:
			doc = mistletoe.Document(content)
			rewrite_children(doc)
			return renderer.render(doc)


if __name__ == '__main__':
	print(rewrite_markdown(
		'abc\n\nfoobar ![asd](1.png)\n\n![bsd](2.md)\n\n![xxx](https://axsx.aaxx/x.txt)\n\n## 12',
		'https://example.com/repos',
		'https://example.com/raw',
	))