# -*- coding: UTF-8 -*-
# This video extraction code based on youtube-dl: https://github.com/ytdl-org/youtube-dl
# From Taapat plugin https://github.com/Taapat/enigma2-plugin-youtube

from __future__ import print_function
from __future__ import absolute_import

from re import escape
from re import findall
from re import match
from re import search
from json import dumps
from json import loads

from Components.config import config

from .compat import compat_parse_qs
from .compat import compat_str
from .compat import compat_Request
from .compat import compat_urlopen
from .compat import compat_URLError
from .compat import SUBURI
from .jsinterp import JSInterpreter


PRIORITY_VIDEO_FORMAT = ()


def create_priority_formats():
	global PRIORITY_VIDEO_FORMAT
	itag = config.plugins.tmbd_yttrailer.best_resolution.value
	video_formats = (
		('17', '91', '13', '151', '160', '269'),  # 176x144
		('5', '36', '92', '132', '133', '229'),  # 400x240
		('18', '93', '34', '6', '134', '230'),  # 640x360
		('35', '59', '78', '94', '135', '212', '231'),  # 854x480
		('22', '95', '300', '136', '298', '232'),  # 1280x720
		('37', '96', '301', '137', '299', '248', '303', '271', '270'),  # 1920x1080
		('38', '266', '264', '138', '313', '315', '272', '308')  # 4096x3072
	)
	for video_format in video_formats:
		PRIORITY_VIDEO_FORMAT = video_format + PRIORITY_VIDEO_FORMAT
		if video_format[0] == itag:
			break


create_priority_formats()


class YouTubeVideoUrl():
	def __init__(self):
		self.use_dash_mp4 = ()
		self._code_cache = {}
		self._player_cache = {}
		self.nsig_cache = (None, None)

	@staticmethod
	def try_get(src, get, expected_type=None):
		try:
			v = get(src)
		except (AttributeError, KeyError, TypeError, IndexError):
			pass
		else:
			if expected_type is None or isinstance(v, expected_type):
				return v

	@staticmethod
	def _guess_encoding_from_content(content_type, webpage_bytes):
		m = match(r'[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\s*;\s*charset=(.+)', content_type)
		if m:
			encoding = m.group(1)
		else:
			m = search(br'<meta[^>]+charset=[\'"]?([^\'")]+)[ /\'">]', webpage_bytes[:1024])
			if m:
				encoding = m.group(1).decode('ascii')
			elif webpage_bytes.startswith(b'\xff\xfe'):
				encoding = 'utf-16'
			else:
				encoding = 'utf-8'

		return encoding

	def _download_webpage(self, url, data=None, headers={}):
		""" Return the data of the page as a string """

		if data:
			data = dumps(data).encode('utf8')
		if data or headers:
			url = compat_Request(url, data=data, headers=headers)
			url.get_method = lambda: 'POST'

		try:
			urlh = compat_urlopen(url, timeout=5)
		except compat_URLError as e:  # pragma: no cover
			raise RuntimeError(e.reason)

		content_type = urlh.headers.get('Content-Type', '')
		webpage_bytes = urlh.read()
		encoding = self._guess_encoding_from_content(content_type, webpage_bytes)

		try:
			content = webpage_bytes.decode(encoding, 'replace')
		except Exception:  # pragma: no cover
			content = webpage_bytes.decode('utf-8', 'replace')

		return content

	@staticmethod
	def _extract_n_function_name(jscode):
		func_name, idx = search(
			r'''(?x)
				(?:
					\.get\("n"\)\)&&\(b=|
					(?:
						b=String\.fromCharCode\(110\)|
						(?P<str_idx>[a-zA-Z0-9_$.]+)&&\(b="nn"\[\+(?P=str_idx)\]
					)
					(?:
						,[a-zA-Z0-9_$]+\(a\))?,c=a\.
						(?:
							get\(b\)|
							[a-zA-Z0-9_$]+\[b\]\|\|null
						)\)&&\(c=|
					\b(?P<var>[a-zA-Z0-9_$]+)=
				)(?P<nfunc>[a-zA-Z0-9_$]+)(?:\[(?P<idx>\d+)\])?\([a-zA-Z]\)
				(?(var),[a-zA-Z0-9_$]+\.set\("n"\,(?P=var)\),(?P=nfunc)\.length)
			''', jscode
		).group('nfunc', 'idx')
		if not func_name:
			print('[YouTubeVideoUrl] Falling back to generic n function search')
			return search(
				r'''(?xs)
					(?:(?<=[^\w$])|^)       # instead of \b, which ignores $
					(?P<name>(?!\d)[a-zA-Z\d_$]+)\s*=\s*function\((?!\d)[a-zA-Z\d_$]+\)
					\s*\{(?:(?!};).)+?["']enhanced_except_
				''', jscode
			).group('name')
		if not idx:
			return func_name
		if int(idx) == 0:
			real_nfunc = search(
				r'var %s\s*=\s*\[([a-zA-Z_$][\w$]*)\];' % (escape(func_name), ),
				jscode
			)
			if real_nfunc:
				return real_nfunc.group(1)

	def _extract_player_info(self):
		res = self._download_webpage('https://www.youtube.com/iframe_api')
		if res:
			player_id = search(r'player\\?/([0-9a-fA-F]{8})\\?/', res)
			if player_id:
				return player_id.group(1)
		print('[YouTubeVideoUrl] Cannot get player info')

	def _load_player(self, player_id):
		if player_id and player_id not in self._player_cache:
			self._player_cache[player_id] = self._download_webpage(
				'https://www.youtube.com/s/player/%s/player_ias.vflset/en_US/base.js' % player_id
			)

	def _extract_function(self, player_id, s_id):
		if player_id not in self._player_cache:
			self._load_player(player_id)
		jsi = JSInterpreter(self._player_cache[player_id])
		if s_id not in self._code_cache:
			if s_id.startswith('nsig_'):
				funcname = self._extract_n_function_name(self._player_cache[player_id])
			else:
				funcname = self._parse_sig_js(self._player_cache[player_id])
			self._code_cache[s_id] = jsi.extract_function_code(funcname)
		return lambda s: jsi.extract_function_from_code(*self._code_cache[s_id])([s])

	def _unthrottle_url(self, url, player_id):
		n_param = search(r'&n=(.+?)&', url).group(1)
		n_id = 'nsig_%s_%s' % (player_id, '.'.join(str(len(p)) for p in n_param.split('.')))
		print('[YouTubeVideoUrl] Decrypt nsig', n_id)
		if self.nsig_cache[0] != n_param:
			self.nsig_cache = (None, None)
			try:
				ret = self._extract_function(player_id, n_id)(n_param)
			except Exception as ex:
				print('[YouTubeVideoUrl] Unable to decode nsig', ex)
			else:
				if ret.startswith('enhanced_except_'):
					print('[YouTubeVideoUrl] Unhandled exception in decode', ret)
				else:
					self.nsig_cache = (n_param, ret)
		if self.nsig_cache[1]:
			print('[YouTubeVideoUrl] Decrypted nsig %s => %s' % self.nsig_cache)
			return url.replace(self.nsig_cache[0], self.nsig_cache[1])
		if n_id in self._code_cache:
			del self._code_cache[n_id]
		return url

	def _decrypt_signature(self, s, player_id):
		"""Turn the encrypted s field into a working signature"""
		s_id = 'sig_%s_%s' % (player_id, '.'.join(str(len(p)) for p in s.split('.')))
		print('[YouTubeVideoUrl] Decrypt signature', s_id)
		try:
			return self._extract_function(player_id, s_id)(s)
		except Exception as ex:
			print('[YouTubeVideoUrl] Signature extraction failed', ex)
			if s_id in self._code_cache:
				del self._code_cache[s_id]

	def _parse_sig_js(self, jscode):

		def _search_regex(pattern, string):
			mobj = ''
			for p in pattern:
				mobj = search(p, string, 0)
				if mobj:
					break
			return mobj

		return _search_regex(
			(r'\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(',
				r'\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(',
				r'\bm=(?P<sig>[a-zA-Z0-9$]{2,})\(decodeURIComponent\(h\.s\)\)',
				r'\bc&&\(c=(?P<sig>[a-zA-Z0-9$]{2,})\(decodeURIComponent\(c\)\)',
				r'(?:\b|[^a-zA-Z0-9$])(?P<sig>[a-zA-Z0-9$]{2,})\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)(?:;[a-zA-Z0-9$]{2}\.[a-zA-Z0-9$]{2}\(a,\d+\))?',
				r'(?P<sig>[a-zA-Z0-9$]+)\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)',
				# Obsolete patterns
				r'("|\')signature\1\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
				r'\.sig\|\|(?P<sig>[a-zA-Z0-9$]+)\(',
				r'yt\.akamaized\.net/\)\s*\|\|\s*.*?\s*[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?:encodeURIComponent\s*\()?\s*(?P<sig>[a-zA-Z0-9$]+)\(',
				r'\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
				r'\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
				r'\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\('),
			jscode
		).group('sig')

	@staticmethod
	def _parse_m3u8_attributes(attrib):
		return {key: val[1:-1] if val.startswith('"') else val for (key, val) in findall(r'(?P<key>[A-Z0-9-]+)=(?P<val>"[^"]+"|[^",]+)(?:,|$)', attrib)}

	def _get_m3u8_audio_urls(self, manifest):
		audio_urls = {}
		if '#EXT-X-MEDIA:' in manifest:
			for line in manifest.splitlines():
				if line.startswith('#EXT-X-MEDIA:'):
					audio_info = self._parse_m3u8_attributes(line)
					audio_urls[audio_info.get('GROUP-ID')] = audio_info.get('URI')
		return audio_urls

	def _extract_from_m3u8(self, manifest_url):
		url_map = {}
		audio_url = ''

		manifest = self._download_webpage(manifest_url)
		audio_urls = self._get_m3u8_audio_urls(manifest)

		for line in manifest.splitlines():
			if audio_urls and line.startswith('#EXT-X-STREAM-INF:'):
				audio_id = self._parse_m3u8_attributes(line).get('AUDIO')
				if audio_id and audio_id in audio_urls:
					audio_url = SUBURI + audio_urls.get(audio_id)
			elif line.startswith('https'):
				itag = search(r'/sgovp/[^/]+itag%3D(\d+?)/', line) or search(r'/itag/(\d+?)/', line)
				if itag:
					url_map[itag.group(1)] = line + audio_url
					audio_url = ''
		return url_map

	def _not_in_fmt(self, fmt, itag):
		return not (
			fmt.get('targetDurationSec') or
			fmt.get('drmFamilies') or
			fmt.get('type') == 'FORMAT_STREAM_TYPE_OTF' or
			itag in self.use_dash_mp4
		)

	def _extract_url(self, our_format, streaming_formats, player_id):
		for fmt in streaming_formats:
			itag = str(fmt.get('itag', ''))
			if itag == our_format and self._not_in_fmt(fmt, itag):
				url = fmt.get('url')
				if not url and 'signatureCipher' in fmt:
					sc = compat_parse_qs(fmt.get('signatureCipher', ''))
					sig = self._decrypt_signature(sc['s'][0], player_id)
					if sig:
						url = '%s&%s=%s' % (sc['url'][0], sc['sp'][0] if 'sp' in sc else 'signature', sig)
				if url:
					if '&n=' in url:
						url = self._unthrottle_url(url, player_id)
					return url

	def _extract_fmt_video_format(self, streaming_formats, player_id):
		""" Find the best format from our format priority map """
		print('[YouTubeVideoUrl] Try fmt url')
		for our_format in PRIORITY_VIDEO_FORMAT:
			url = self._extract_url(our_format, streaming_formats, player_id)
			if url:
				print('[YouTubeVideoUrl] Found fmt url')
				return url, our_format
		return '', ''

	def _extract_dash_audio_format(self, streaming_formats, player_id):
		""" If DASH MP4 video add link also on Dash MP4 Audio """
		print('[YouTubeVideoUrl] Try fmt audio url')
		for our_format in ('141', '140', '139', '258', '265', '325', '328', '233', '234'):
			url = self._extract_url(our_format, streaming_formats, player_id)
			if url:
				print('[YouTubeVideoUrl] Found fmt audio url')
				return url
		return ''

	def _extract_web_response(self, video_id):
		url = 'https://www.youtube.com/watch?v=%s&bpctr=9999999999&has_verified=1' % video_id
		webpage = self._download_webpage(url)
		if webpage:
			player_response = search(r'ytInitialPlayerResponse\s*=\s*({[^>]*})\s*;\s*(?:var\s+meta|</script|\n)', webpage)
			if player_response:
				try:
					return loads(player_response.group(1)), self._extract_player_info()
				except ValueError:  # pragma: no cover
					print('[YouTubeVideoUrl] Failed to parse web JSON')
		return None, None

	def _extract_player_response(self, video_id, yt_auth, client):
		player_id = None
		url = 'https://www.youtube.com/youtubei/v1/player?prettyPrint=false'
		data = {
			'videoId': video_id,
			'playbackContext': {
				'contentPlaybackContext': {
					'html5Preference': 'HTML5_PREF_WANTS'
				}
			}
		}
		headers = {
			'content-type': 'application/json',
			'Origin': 'https://www.youtube.com',
			'X-YouTube-Client-Name': client
		}
		if yt_auth:
			headers['Authorization'] = yt_auth
		if client == 5:
			VERSION = '19.29.1'
			USER_AGENT = 'com.google.ios.youtube/%s (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)' % VERSION
			data['context'] = {
				'client': {
					'hl': 'en',
					'clientVersion': VERSION,
					'clientName': 'IOS',
					'deviceMake': 'Apple',
					'deviceModel': 'iPhone16,2',
					'osName': 'iPhone',
					'osVersion': '17.5.1.21F90',
					'userAgent': USER_AGENT
				}
			}
			headers['X-YouTube-Client-Version'] = VERSION
			headers['User-Agent'] = USER_AGENT
		elif client == 85:
			player_id = self._extract_player_info()
			if player_id:
				if player_id not in self._player_cache:
					self._load_player(player_id)
				sts = search(
					r'(?:signatureTimestamp|sts)\s*:\s*(?P<sts>\d{5})',
					self._player_cache[player_id]
				).group('sts')
				if sts:
					data['playbackContext']['contentPlaybackContext']['signatureTimestamp'] = sts
			data['context'] = {
				'client': {
					'hl': 'en',
					'clientName': 'TVHTML5_SIMPLY_EMBEDDED_PLAYER',
					'clientVersion': '2.0',
				},
				'thirdParty': {
					'embedUrl': 'https://www.youtube.com/'
				}
			}
			headers['X-YouTube-Client-Version'] = '2.0'
		else:
			VERSION = '19.29.37'
			USER_AGENT = 'com.google.android.youtube/%s (Linux; U; Android 11) gzip' % VERSION
			data['context'] = {
				'client': {
					'hl': 'en',
					'clientVersion': VERSION,
					'androidSdkVersion': 30,
					'clientName': 'ANDROID',
					'osName': 'Android',
					'osVersion': '11',
					'userAgent': USER_AGENT
				}
			}
			data['params'] = '2AMB'
			headers['X-YouTube-Client-Version'] = VERSION
			headers['User-Agent'] = USER_AGENT
		try:
			return loads(self._download_webpage(url, data, headers)), player_id
		except ValueError:  # pragma: no cover
			print('[YouTubeVideoUrl] Failed to parse JSON')
			return None, None

	def _real_extract(self, video_id, yt_auth):
		IGNORE_VIDEO_FORMAT = (
			'43', '44', '45', '46',  # webm
			'82', '83', '84', '85',  # 3D
			'100', '101', '102',  # 3D
			'167', '168', '169',  # webm
			'170', '171', '172',  # webm
			'218', '219',  # webm
			'242', '243', '244', '245', '246', '247',  # webm
			'394', '395', '396', '397', '398', '399', '400', '401', '402', '694', '695', '696', '697', '698', '699', '700', '701', '571',  # AV1
			'249', '250', '251',  # webm
			'302'  # webm
		)
		DASHMP4_FORMAT = (
			'133', '134', '135', '136', '137', '138', '160',
			'212', '229', '230', '231', '232', '248', '264',
			'271', '272', '266', '269', '270', '298', '299',
			'303', '313', '315', '308'
		)
		url = ''

		if config.plugins.tmbd_yttrailer.useDashMP4.value:
			self.use_dash_mp4 = ()
		else:
			print('[YouTubeVideoUrl] skip DASH MP4 format')
			self.use_dash_mp4 = DASHMP4_FORMAT

		player_response, player_id = self._extract_player_response(video_id, yt_auth, 3)
		if not player_response:
			raise RuntimeError('Player response not found!')

		if self.try_get(player_response, lambda x: x['videoDetails']['videoId']) != video_id:
			if self.use_dash_mp4:
				print('[YouTubeVideoUrl] Got wrong player response, try web response')
				player_response, player_id = self._extract_web_response(video_id)
			else:
				print('[YouTubeVideoUrl] Got wrong player response, try ios client')
				player_response, player_id = self._extract_player_response(video_id, yt_auth, 5)

		is_live = self.try_get(player_response, lambda x: x['videoDetails']['isLive'])
		playability_status = player_response.get('playabilityStatus', {})

		if not is_live and playability_status.get('status') == 'LOGIN_REQUIRED':
			print('[YouTubeVideoUrl] Age gate content')
			player_response, player_id = self._extract_player_response(video_id, yt_auth, 85)
			if not player_response:
				raise RuntimeError('Age gate content player response not found!')

			playability_status = player_response.get('playabilityStatus', {})

		trailer_video_id = self.try_get(
			playability_status,
			lambda x: x['errorScreen']['playerLegacyDesktopYpcTrailerRenderer']['trailerVideoId'],
			compat_str
		)
		if trailer_video_id:
			print('[YouTubeVideoUrl] Trailer video')
			return str(trailer_video_id)

		streaming_data = player_response.get('streamingData', {})
		streaming_formats = streaming_data.get('formats', [])

		# If priority format changed in config, recreate priority list
		if PRIORITY_VIDEO_FORMAT[0] != config.plugins.tmbd_yttrailer.best_resolution.value:
			create_priority_formats()

		if not is_live:
			streaming_formats.extend(streaming_data.get('adaptiveFormats', []))
			url, our_format = self._extract_fmt_video_format(streaming_formats, player_id)
			if url and our_format in DASHMP4_FORMAT:
				audio_url = self._extract_dash_audio_format(streaming_formats, player_id)
				if audio_url:
					url += SUBURI + audio_url
			if not url:  # pragma: no cover
				for fmt in streaming_formats:
					itag = str(fmt.get('itag', ''))
					if itag not in IGNORE_VIDEO_FORMAT and self._not_in_fmt(fmt, itag):
						url = fmt.get('url')
						if url:
							break
			if not url and streaming_formats:  # pragma: no cover
				url = streaming_formats[0].get('url', '')

		if not url:
			print('[YouTubeVideoUrl] Try manifest url')
			hls_manifest_url = streaming_data.get('hlsManifestUrl')
			if hls_manifest_url:
				url_map = self._extract_from_m3u8(hls_manifest_url)

				# Find the best format from our format priority map
				for our_format in PRIORITY_VIDEO_FORMAT:
					if our_format in url_map:
						url = url_map[our_format]
						break
				# If anything not found, used first in the list if it not in ignore map
				if not url:  # pragma: no cover
					for url_map_key in list(url_map.keys()):
						if url_map_key not in IGNORE_VIDEO_FORMAT:
							url = url_map[url_map_key]
							break
				if not url and url_map:  # pragma: no cover
					url = list(url_map.values())[0]

		if not url:
			reason = playability_status.get('reason')
			if reason:
				subreason = playability_status.get('messages')
				if subreason:
					if isinstance(subreason, list):
						subreason = subreason[0]
					reason += '\n%s' % subreason
			raise RuntimeError(reason)

		return str(url)

	def extract(self, video_id, yt_auth=None):
		error_message = None
		for _ in range(3):
			try:
				return self._real_extract(video_id, yt_auth)
			except Exception as ex:
				if ex is None:
					print('No supported formats found, trying again!')
				else:
					error_message = str(ex)
					break
		if not error_message:
			error_message = 'No supported formats found in video info!'
		raise RuntimeError(error_message)
