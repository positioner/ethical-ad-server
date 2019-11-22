"""Ad server views."""
import collections
import logging
from datetime import datetime
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView
from user_agents import parse as parse_user_agent

from .constants import CAMPAIGN_TYPES
from .constants import CLICKS
from .constants import VIEWS
from .models import AdImpression
from .models import Advertisement
from .models import Advertiser
from .models import Publisher
from .utils import analytics_event
from .utils import calculate_ctr
from .utils import calculate_ecpm
from .utils import get_ad_day
from .utils import get_client_ip
from .utils import get_client_user_agent
from .utils import get_geolocation
from .utils import is_blacklisted_user_agent
from .utils import is_click_ratelimited


log = logging.getLogger(__name__)  # noqa


def do_not_track(request):
    """
    Returns the Do Not Track status for the user.

    https://w3c.github.io/dnt/drafts/tracking-dnt.html#status-representation

    :raises: Http404 if ``settings.ADSERVER_DO_NOT_TRACK`` is ``False``
    """
    if not settings.ADSERVER_DO_NOT_TRACK:
        raise Http404

    dnt_header = request.META.get("HTTP_DNT")

    data = {"tracking": "N" if dnt_header == "1" else "T"}
    if settings.ADSERVER_PRIVACY_POLICY_URL:
        data["policy"] = settings.ADSERVER_PRIVACY_POLICY_URL

    # pylint: disable=redundant-content-type-for-json-response
    return JsonResponse(data, content_type="application/tracking-status+json")


def do_not_track_policy(request):
    """
    Returns the Do Not Track policy.

    https://github.com/EFForg/dnt-guide#12-how-to-assert-dnt-compliance

    :raises: Http404 if ``settings.ADSERVER_DO_NOT_TRACK`` is ``False``
    """
    if not settings.ADSERVER_DO_NOT_TRACK:
        raise Http404

    return render(request, "adserver/dnt-policy.txt", content_type="text/plain")


@login_required
def dashboard(request):
    """The initial dashboard view."""
    if request.user.is_staff:
        publishers = Publisher.objects.all()
        advertisers = Advertiser.objects.all()
    else:
        publishers = request.user.publishers.all()
        advertisers = request.user.advertisers.all()

    return render(
        request,
        "adserver/dashboard.html",
        {"advertisers": advertisers, "publishers": publishers},
    )


class BaseProxyView(View):

    """A base view for proxying ad views and clicks and collecting relevant metrics on clicks and views."""

    log_level = logging.DEBUG
    log_security_level = logging.WARNING
    impression_type = VIEWS
    success_message = "Billed impression"

    def ignore_tracking_reason(self, request, advertisement, nonce, publisher):
        """Returns a reason this impression should not be tracked or `None` if this *should* be tracked."""
        reason = None

        ip_address = get_client_ip(request)
        user_agent = get_client_user_agent(request)
        parsed_ua = parse_user_agent(user_agent)

        country_code = None
        geo_data = get_geolocation(ip_address)
        if geo_data:
            country_code = geo_data["country_code"]

        valid_nonce = advertisement.is_valid_nonce(self.impression_type, nonce)

        if not valid_nonce:
            log.log(self.log_level, "Old or nonexistent impression nonce")
            reason = "Old/Nonexistent nonce"
        elif parsed_ua.is_bot:
            log.log(self.log_level, "Bot impression. User Agent: [%s]", user_agent)
            reason = "Bot impression"
        elif ip_address in settings.INTERNAL_IPS:
            log.log(
                self.log_level, "Internal IP impression. User Agent: [%s]", user_agent
            )
            reason = "Internal IP"
        elif parsed_ua.os.family == "Other" and parsed_ua.browser.family == "Other":
            # This is probably a bot/proxy server/prefetcher/etc.
            log.log(self.log_level, "Unknown user agent impression [%s]", user_agent)
            reason = "Unrecognized user agent"
        elif request.user.is_staff:
            log.log(self.log_level, "Ignored staff user ad impression")
            reason = "Staff impression"
        elif is_blacklisted_user_agent(user_agent):
            log.log(
                self.log_level, "Blacklisted user agent impression [%s]", user_agent
            )
            reason = "Blacklisted impression"
        elif not publisher:
            log.log(self.log_level, "Ad impression for unknown publisher")
            reason = "Unknown publisher"
        elif not advertisement.flight.show_to_geo(country_code):
            # This is very rare but it is visible in ad reports
            # I believe the most common cause for this is somebody uses a VPN and is served an ad
            # Then they turn off their VPN and click on the ad
            log.log(
                self.log_security_level,
                "Invalid geo targeting for ad [%s]. Country: [%s]",
                advertisement,
                country_code,
            )
            reason = "Invalid targeting impression"
        elif self.impression_type == CLICKS and is_click_ratelimited(request):
            # Note: Normally logging IPs is frowned upon for DNT
            # but this is a security/billing violation
            log.log(
                self.log_security_level,
                "User has clicked too many ads recently, IP = [%s], User Agent = [%s]",
                ip_address,
                user_agent,
            )
            reason = "Ratelimited impression"

        return reason

    def send_to_analytics(self, request, advertisement, message):
        """A no-op by default, sublcasses may override it to send view/clicks to analytics."""

    def get(self, request, advertisement_id, nonce):
        """Handles proxying ad views and clicks and collecting metrics on them."""
        advertisement = get_object_or_404(Advertisement, pk=advertisement_id)
        publisher = advertisement.get_publisher(nonce)
        referrer = request.META.get("HTTP_REFERER")

        ignore_reason = self.ignore_tracking_reason(
            request, advertisement, nonce, publisher
        )

        if not ignore_reason:
            log.log(self.log_level, self.success_message)
            advertisement.invalidate_nonce(self.impression_type, nonce)
            advertisement.track_impression(
                request, self.impression_type, publisher, referrer
            )

        message = ignore_reason or self.success_message
        response = self.get_response(request, advertisement)

        # Add the reason for accepting or rejecting the impression to the headers
        # but only for staff or in DEBUG/TESTING
        if settings.DEBUG or settings.TESTING or request.user.is_staff:
            response["X-Adserver-Reason"] = message

        return response

    def get_response(self, request, advertisement):
        """Subclasses *must* override this method."""
        raise NotImplementedError


class AdViewProxyView(BaseProxyView):

    """Track an ad view."""

    impression_type = VIEWS
    success_message = "Billed view"

    def get_response(self, request, advertisement):
        return HttpResponse(
            "<svg><!-- View Proxy --></svg>", content_type="image/svg+xml"
        )


class AdClickProxyView(BaseProxyView):

    """Track an ad click and redirect to the ad destination link."""

    impression_type = CLICKS
    success_message = "Billed click"

    def send_to_analytics(self, request, advertisement, message):
        ip_address = get_client_ip(request)
        user_agent = get_client_user_agent(request)

        event_category = "Advertisement"
        event_label = advertisement.slug
        event_action = message

        # The event_value is in US cents (eg. for $2 CPC, the value is 200)
        # CPMs are too small to register
        event_value = int(advertisement.flight.cpc * 100)

        analytics_event(
            event_category=event_category,
            event_action=event_action,
            event_label=event_label,
            event_value=event_value,
            ua=user_agent,
            uip=ip_address,  # will be anonymized
        )

    def get_response(self, request, advertisement):
        return HttpResponseRedirect(advertisement.link)


class BaseReportView(UserPassesTestMixin, TemplateView):

    """
    A base report that other reports can extend.

    By default, it restricts access to staff and sets up date context variables.
    """

    DEFAULT_REPORT_DAYS = 30

    def test_func(self):
        """By default, reports are locked down to staff."""
        return self.request.user.is_staff

    def get_context_data(self, **kwargs):
        start_date = self.get_start_date()
        end_date = self.get_end_date()
        campaign_type = self.request.GET.get("campaign_type", "")

        if end_date and end_date < start_date:
            end_date = None

        return {
            "start_date": start_date,
            "end_date": end_date,
            "campaign_type": campaign_type,
        }

    def _parse_date_string(self, date_str):
        try:
            return timezone.make_aware(datetime.strptime(date_str, "%Y-%m-%d"))
        except ValueError:
            # Since this can come from GET params, handle errors
            pass

        return None

    def get_start_date(self):
        if "start_date" in self.request.GET:
            start_date = self._parse_date_string(self.request.GET["start_date"])
            if start_date:
                return start_date

        return get_ad_day() - timedelta(days=self.DEFAULT_REPORT_DAYS)

    def get_end_date(self):
        if "end_date" in self.request.GET:
            end_date = self._parse_date_string(self.request.GET["end_date"])
            if end_date:
                return end_date

        return None


class AdvertiserReportView(BaseReportView):

    """A report for one advertiser."""

    template_name = "adserver/reports/advertiser.html"

    def test_func(self):
        """The user must have access on the advertiser or be staff."""
        if self.request.user.is_anonymous:
            return False

        advertiser = get_object_or_404(Advertiser, slug=self.kwargs["advertiser_slug"])
        return (
            self.request.user.is_staff
            or advertiser in self.request.user.advertisers.all()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data()

        advertiser_slug = kwargs.get("advertiser_slug", "")

        advertiser = get_object_or_404(Advertiser, slug=advertiser_slug)
        advertisements = Advertisement.objects.filter(
            flight__campaign__advertiser=advertiser
        )
        advertisements = advertisements.select_related(
            "flight", "flight__campaign", "flight__campaign"
        )

        ads_and_reports = []
        for ad in advertisements:
            report = ad.daily_reports(
                start_date=context["start_date"], end_date=context["end_date"]
            )
            if report["total"]["views"]:
                ads_and_reports.append((ad, report))

        advertiser_report = advertiser.daily_reports(
            start_date=context["start_date"], end_date=context["end_date"]
        )

        context.update(
            {
                "advertiser": advertiser,
                "advertiser_report": advertiser_report,
                "ads_and_reports": ads_and_reports,
                "total_clicks": advertiser_report["total"]["clicks"],
                "total_cost": advertiser_report["total"]["cost"],
                "total_views": advertiser_report["total"]["views"],
                "total_ctr": advertiser_report["total"]["ctr"],
            }
        )

        return context


class AllAdvertiserReportView(BaseReportView):

    """A report aggregating all advertisers."""

    template_name = "adserver/reports/all-advertisers.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data()

        # Get all advertisers where an ad for that advertiser has a view or click
        # in the specified date range
        impressions = AdImpression.objects.filter(date__gte=context["start_date"])
        if context["end_date"]:
            impressions = impressions.filter(date__lte=context["end_date"])
        advertisers = Advertiser.objects.filter(
            id__in=Advertisement.objects.filter(
                id__in=impressions.values("advertisement")
            ).values("flight__campaign__advertiser")
        )

        advertisers_and_reports = []
        for advertiser in advertisers:
            report = advertiser.daily_reports(
                start_date=context["start_date"], end_date=context["end_date"]
            )
            if report["total"]["views"] > 0:
                advertisers_and_reports.append((advertiser, report))

        total_clicks = sum(
            report["total"]["clicks"] for _, report in advertisers_and_reports
        )
        total_views = sum(
            report["total"]["views"] for _, report in advertisers_and_reports
        )
        total_cost = sum(
            report["total"]["cost"] for _, report in advertisers_and_reports
        )

        # Aggregate the different advertiser reports by day
        days = {}
        for advertiser, report in advertisers_and_reports:
            for day in report["days"]:
                if day["date"] not in days:
                    days[day["date"]] = collections.defaultdict(int)
                    days[day["date"]]["views_by_advertiser"] = {}
                    days[day["date"]]["clicks_by_advertiser"] = {}

                days[day["date"]]["date"] = day["date"].strftime("%Y-%m-%d")
                days[day["date"]]["views"] += day["views"]
                days[day["date"]]["clicks"] += day["clicks"]
                days[day["date"]]["views_by_advertiser"][advertiser.name] = day["views"]
                days[day["date"]]["clicks_by_advertiser"][advertiser.name] = day[
                    "clicks"
                ]
                days[day["date"]]["cost"] += float(day["cost"])
                days[day["date"]]["ctr"] = calculate_ctr(
                    days[day["date"]]["clicks"], days[day["date"]]["views"]
                )

        context.update(
            {
                "advertisers": [a for a, _ in advertisers_and_reports],
                "advertisers_and_reports": advertisers_and_reports,
                "total_clicks": total_clicks,
                "total_cost": total_cost,
                "total_views": total_views,
                "total_ctr": calculate_ctr(total_clicks, total_views),
                "total_ecpm": calculate_ecpm(total_cost, total_views),
            }
        )

        return context


class PublisherReportView(BaseReportView):

    """A report for a single publisher."""

    template_name = "adserver/reports/publisher.html"

    def test_func(self):
        """The user must have access on the publisher or be staff."""
        if self.request.user.is_anonymous:
            return False

        publisher = get_object_or_404(Publisher, slug=self.kwargs["publisher_slug"])
        return (
            self.request.user.is_staff
            or publisher in self.request.user.publishers.all()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data()

        publisher_slug = kwargs.get("publisher_slug", "")
        publisher = get_object_or_404(Publisher, slug=publisher_slug)

        report = publisher.daily_reports(
            start_date=context["start_date"], end_date=context["end_date"]
        )

        context.update({"publisher": publisher, "report": report})

        return context


class AllPublisherReportView(BaseReportView):

    """A report for all publishers."""

    template_name = "adserver/reports/all-publishers.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data()

        # Get all publishers where an ad has a view or click in the specified date range
        impressions = AdImpression.objects.filter(date__gte=context["start_date"])
        if context["end_date"]:
            impressions = impressions.filter(date__lte=context["end_date"])
        if context["campaign_type"] and context["campaign_type"] in CAMPAIGN_TYPES:
            impressions = impressions.filter(
                advertisement__flight__campaign__campaign_type=context["campaign_type"]
            )
        publishers = Publisher.objects.filter(id__in=impressions.values("publisher"))

        publishers_and_reports = []
        for publisher in publishers:
            report = publisher.daily_reports(
                start_date=context["start_date"], end_date=context["end_date"]
            )
            if report["total"]["views"] > 0:
                publishers_and_reports.append((publisher, report))

        total_clicks = sum(
            report["total"]["clicks"] for _, report in publishers_and_reports
        )
        total_views = sum(
            report["total"]["views"] for _, report in publishers_and_reports
        )
        total_cost = sum(
            report["total"]["cost"] for _, report in publishers_and_reports
        )

        # Aggregate the different publisher reports by day
        days = {}
        for publisher, report in publishers_and_reports:
            for day in report["days"]:
                if day["date"] not in days:
                    days[day["date"]] = collections.defaultdict(int)
                    days[day["date"]]["views_by_publisher"] = {}
                    days[day["date"]]["clicks_by_publisher"] = {}

                days[day["date"]]["date"] = day["date"].strftime("%Y-%m-%d")
                days[day["date"]]["views"] += day["views"]
                days[day["date"]]["clicks"] += day["clicks"]
                days[day["date"]]["views_by_publisher"][publisher.name] = day["views"]
                days[day["date"]]["clicks_by_publisher"][publisher.name] = day["clicks"]
                days[day["date"]]["cost"] += float(day["cost"])
                days[day["date"]]["ctr"] = calculate_ctr(
                    days[day["date"]]["clicks"], days[day["date"]]["views"]
                )

        context.update(
            {
                "publishers": [p for p, _ in publishers_and_reports],
                "publishers_and_reports": publishers_and_reports,
                "total_clicks": total_clicks,
                "total_cost": total_cost,
                "total_views": total_views,
                "total_ctr": calculate_ctr(total_clicks, total_views),
                "total_ecpm": calculate_ecpm(total_cost, total_views),
                "campaign_types": CAMPAIGN_TYPES,
            }
        )

        return context
