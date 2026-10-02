"""Ad and tracking domains the Web tab blocks on other sites' pages
(pages/web/browser.py is_tracker): the big ad networks, analytics and
session-recording services, and social-media trackers. A domain covers
everything under it. Hand-picked, so it's short and nothing on a site's
own domain is ever caught - it's a lighter page, not a full ad blocker."""

BLOCKED = frozenset("""
doubleclick.net googlesyndication.com googleadservices.com google-analytics.com googletagmanager.com
googletagservices.com adservice.google.com pagead2.googlesyndication.com
adnxs.com adsrvr.org amazon-adsystem.com criteo.com criteo.net taboola.com outbrain.com
rubiconproject.com pubmatic.com openx.net casalemedia.com indexww.com smartadserver.com
adform.net yieldmo.com sharethrough.com triplelift.com 3lift.com teads.tv media.net
moatads.com doubleverify.com adsafeprotected.com serving-sys.com sizmek.com bidswitch.net
contextweb.com districtm.io gumgum.com lijit.com sovrn.com spotxchange.com spotx.tv
advertising.com adtechus.com zemanta.com revcontent.com mgid.com
scorecardresearch.com quantserve.com quantcount.com comscore.com chartbeat.com chartbeat.net
hotjar.com hotjar.io mouseflow.com fullstory.com crazyegg.com luckyorange.com clarity.ms
mixpanel.com segment.io segment.com amplitude.com heap.io heapanalytics.com newrelic.com nr-data.net
optimizely.com kissmetrics.com statcounter.com histats.com mc.yandex.ru
connect.facebook.net facebook.net ads.linkedin.com px.ads.linkedin.com analytics.twitter.com
ads-twitter.com static.ads-twitter.com ads.pinterest.com ct.pinterest.com analytics.tiktok.com
bat.bing.com ads.yahoo.com analytics.yahoo.com adroll.com perfectaudience.com rlcdn.com
bluekai.com krxd.net demdex.net everesttech.net omtrdc.net exelator.com agkn.com tapad.com
mathtag.com turn.com eyeota.net crwdcntrl.net liadm.com id5-sync.com adsymptotic.com
onetag-sys.com ad.gt brealtime.com undertone.com zedo.com popads.net propellerads.com
exoclick.com juicyads.com trafficjunky.net adcash.com
""".split())
