# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images embedded in page and post HTML: which the source hosts, and pointing the HTML at the copies."""

from __future__ import annotations

from idempotent_importer.html import embedded_images, hosted_on, replace_images

HTML = (
	'<p><img src="https://shop.example.test/a.png?v=1" alt="A">'
	"<img class='x' src='//shop.example.test/b.png'>"
	'<img src="https://elsewhere.example.test/c.png">'
	'<img src="/relative.png"><img src="https://shop.example.test/a.png?v=1"></p>'
	'<IMG SRC="https://www.shop.example.test/d&amp;e.png">'
)


def test_only_the_sources_own_images_are_listed_once_each_in_order() -> None:
	assert embedded_images(HTML, hosted_on("shop.example.test")) == [
		"https://shop.example.test/a.png?v=1",
		"https://shop.example.test/b.png",
		"https://www.shop.example.test/d&e.png",
	]


def test_subdomains_count_as_the_host_and_lookalikes_do_not() -> None:
	owned = hosted_on("www.shop.example.test", "")
	assert owned("https://cdn.shop.example.test/x.png")
	assert not owned("https://evilshop.example.test/x.png")


def test_replacing_points_each_listed_image_at_its_copy_and_leaves_the_rest() -> None:
	replaced = replace_images(
		HTML,
		{
			"https://shop.example.test/a.png?v=1": "/files/a.png",
			"https://shop.example.test/b.png": '/files/b "quoted".png',
			"https://www.shop.example.test/d&e.png": "/files/d.png",
		},
	)
	assert replaced.count('src="/files/a.png"') == 2
	assert "src='/files/b &quot;quoted&quot;.png'" in replaced
	assert 'SRC="/files/d.png"' in replaced
	assert '<img src="https://elsewhere.example.test/c.png">' in replaced
	assert replace_images(HTML, {}) is HTML
