from urllib.parse import quote
import math
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from datetime import datetime
import json, requests
from .models import OpportunityMeetingDetail, CustomerLocationUpdate
from django.contrib.admin.views.decorators import staff_member_required
from .energy import award_points
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, F, Window
from django.db.models.functions import TruncMonth, TruncWeek, Rank
from django.contrib.auth import login, get_user_model
from .models import EnergyPointTransaction
from django.utils.timezone import localtime
from django.http import JsonResponse
from django.conf import settings

def get_employees(request):
    """
    Returns [{"email": "user1@example.com", "label": "Employee Name"} ...]
    for Assign-To autocomplete.
    """
    try:
        sess = requests.Session()
        sess.cookies.update(request.session.get("frappe_cookies", {}))
        token = getattr(settings, "FRAPPE_API_TOKEN", None)
        if token:
            sess.headers.update({"Authorization": f"token {token}"})

        employees = []

        # Get active employees with user_id
        params_emp = {
            "fields": '["name","employee_name","user_id"]',
            "limit_page_length": 1000
        }
        r1 = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Employee", params=params_emp, timeout=10)
        for row in (r1.json().get("data") or []):
            email = (row or {}).get("user_id")
            empname = (row or {}).get("employee_name")
            if email and empname:
                employees.append({
                    "email": email,
                    "label": empname
                })

        # Optional: also include enabled system users without Employee doc
        params_user = {
            "fields": '["name","enabled","full_name"]',
            "filters": '[["enabled","=","1"]]',
            "limit_page_length": 1000
        }
        r2 = sess.get(f"{FRAPPE_BASE_URL}/api/resource/User", params=params_user, timeout=10)
        for u in (r2.json().get("data") or []):
            email = (u or {}).get("name")
            if email and "@" in email:
                label = (u or {}).get("full_name") or email
                employees.append({
                    "email": email,
                    "label": label
                })

        return JsonResponse({"employees": employees})
    except Exception:
        return JsonResponse({"employees": []})




# --- add helper functions somewhere above energy_points() ---
def fiscal_year_bounds(dt):
    """Return (start, end, label) for fiscal year Apr->Mar for the given aware datetime."""
    lt = localtime(dt)
    fy_start_year = lt.year if lt.month >= 4 else lt.year - 1
    start = lt.replace(year=fy_start_year, month=4, day=1, hour=0, minute=0, second=0, microsecond=0)
    # end is exclusive (Apr 1 next year)
    end = start.replace(year=fy_start_year + 1)
    label = f"{str(fy_start_year % 100).zfill(2)}-{str((fy_start_year + 1) % 100).zfill(2)}"
    return start, end, label


def fetch_employee_names_for_usernames(request, usernames):
    """Bulk map Django usernames (emails) -> ERP Employee.employee_name using Frappe filters."""
    if not usernames:
        return {}
    sess = requests.Session()
    # use the logged-in user's ERP cookies or API token
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    # Frappe filter using "in" (batch if needed)
    base_url = f"{FRAPPE_BASE_URL}/api/resource/Employee"
    fields = '["user_id","employee_name"]'
    mapping = {}
    batch = 200
    arr = list({u for u in usernames if u})
    for i in range(0, len(arr), batch):
        part = arr[i:i+batch]
        params = {
            "fields": fields,
            "filters": json.dumps([["user_id", "in", part], ["status", "=", "Active"]]),
            "limit_page_length": 0,
        }
        try:
            r = sess.get(base_url, params=params, timeout=10)
            if r.ok:
                for row in r.json().get("data", []):
                    uid = (row.get("user_id") or "").strip()
                    enm = (row.get("employee_name") or "").strip()
                    if uid and enm and uid not in mapping:
                        mapping[uid] = enm
        except Exception:
            pass
    return mapping



def _display_name(u):
    # Helper to pick a nice name
    full = f"{u.get('user__first_name','').strip()} {u.get('user__last_name','').strip()}".strip()
    return full or u.get("user__username") or f"User {u.get('user')}"


@login_required
@require_http_methods(["GET"])
def energy_points(request):
    if not request.session.get("frappe_user"):
        return redirect(f"{reverse('frappe_login')}?next=/energy/")

    qs = EnergyPointTransaction.objects.filter(user=request.user)

    # Lifetime & recent list (unchanged)
    lifetime = qs.aggregate(s=Sum("points"))["s"] or 0
    txs = qs.order_by("-created_at")[:200]

    # Current week / month (unchanged)
    now = timezone.localtime()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    weekly_total = qs.filter(created_at__gte=week_start).aggregate(s=Sum("points"))["s"] or 0
    monthly_total = qs.filter(created_at__gte=month_start).aggregate(s=Sum("points"))["s"] or 0

    # === Fiscal year (Apr -> next Mar) ===
    fy_start, fy_end, fy_label = fiscal_year_bounds(now)
    fiscal_total = qs.filter(created_at__gte=fy_start, created_at__lt=fy_end).aggregate(s=Sum("points"))["s"] or 0

    # History (limit to FY for monthly; keep weekly last 12 as-is)
    monthly_rollup = (
        qs.filter(created_at__gte=fy_start, created_at__lt=fy_end)
          .annotate(bucket=TruncMonth("created_at"))
          .values("bucket")
          .annotate(points=Sum("points"))
          .order_by("-bucket")[:12]
    )
    weekly_rollup = (
        qs.annotate(bucket=TruncWeek("created_at"))
          .values("bucket")
          .annotate(points=Sum("points"))
          .order_by("-bucket")[:12]
    )

    # ---------- Leaderboards ----------
    def leaderboard(base_qs):
        return (
            base_qs
            .values("user", "user__username", "user__first_name", "user__last_name")
            .annotate(points=Sum("points"))
            .annotate(rank=Window(expression=Rank(), order_by=F("points").desc()))
            .order_by("rank")
        )

    all_qs = EnergyPointTransaction.objects.all()
    month_qs = EnergyPointTransaction.objects.filter(created_at__gte=month_start)
    week_qs  = EnergyPointTransaction.objects.filter(created_at__gte=week_start)
    fy_qs    = EnergyPointTransaction.objects.filter(created_at__gte=fy_start, created_at__lt=fy_end)

    lb_all   = list(leaderboard(all_qs)[:10])
    lb_month = list(leaderboard(month_qs)[:10])
    lb_week  = list(leaderboard(week_qs)[:10])
    lb_fy    = list(leaderboard(fy_qs)[:10])  # NEW

    # Enrich names with ERP Employee.employee_name
    usernames = {r.get("user__username") for r in (lb_all + lb_month + lb_week + lb_fy)}
    emp_map = fetch_employee_names_for_usernames(request, usernames)
    for row in lb_all + lb_month + lb_week + lb_fy:
        uname = row.get("user__username") or ""
        empnm = emp_map.get(uname, "").strip()
        # Show "Employee Name (username)" if available; otherwise fallback to the existing helper
        row["display_name"] = f"{empnm} ({uname})" if empnm and uname else (
            f"{empnm}" if empnm else _display_name(row)
        )

    # Your ranks
    def find_me(lb_qs): return leaderboard(lb_qs).filter(user=request.user.id).first()
    me_all   = find_me(all_qs)
    me_month = find_me(month_qs)
    me_week  = find_me(week_qs)
    me_fy    = find_me(fy_qs)  # NEW

    # "Around me" windows (optional; keep weekly/monthly/all as-is)
    def around_me(lb_qs, me_row, window=2):
        if not me_row: return []
        r = me_row["rank"]
        return list(leaderboard(lb_qs).filter(rank__gte=r-window, rank__lte=r+window).order_by("rank"))

    near_all   = around_me(all_qs, me_all)
    near_month = around_me(month_qs, me_month)
    near_week  = around_me(week_qs, me_week)
    near_fy    = around_me(fy_qs, me_fy)  # NEW

    # Averages
    def avg_points(lb_qs):
        agg = leaderboard(lb_qs).aggregate(total=Sum("points"))
        cnt = leaderboard(lb_qs).count() or 1
        return int((agg["total"] or 0) / cnt)

    avg_all   = avg_points(all_qs)
    avg_month = avg_points(month_qs)
    avg_week  = avg_points(week_qs)
    avg_fy    = avg_points(fy_qs)  # NEW

    # Fetch current user's Employee name for header badge
    employee_name = None
    try:
        sess = requests.Session()
        sess.cookies.update(request.session.get("frappe_cookies", {}))
        if token := getattr(settings, "FRAPPE_API_TOKEN", None):
            sess.headers.update({"Authorization": f"token {token}"})
        params = {
            "fields": '["name","employee_name"]',
            "filters": json.dumps([["user_id", "like", f"{request.session.get('frappe_user','')}%"], ["status","=","Active"]]),
            "limit_page_length": 1,
        }
        r = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Employee", params=params, timeout=8)
        if r.ok:
            data = r.json().get("data") or []
            if data: employee_name = data[0].get("employee_name") or None
    except Exception:
        pass

    return render(request, "energy_points.html", {
        "txs": txs,
        "total": lifetime,

        # week/month existing KPIs
        "weekly_total": weekly_total,
        "monthly_total": monthly_total,
        "week_start": week_start,
        "month_start": month_start,

        # FY KPI + label
        "fiscal_total": fiscal_total,
        "fy_start": fy_start,
        "fy_end": fy_end,
        "fy_label": fy_label,

        # rollups
        "monthly_rollup": monthly_rollup,
        "weekly_rollup": weekly_rollup,

        # leaderboards
        "lb_all": lb_all,
        "lb_month": lb_month,
        "lb_week": lb_week,
        "lb_fy": lb_fy,          # NEW
        "me_all": me_all,
        "me_month": me_month,
        "me_week": me_week,
        "me_fy": me_fy,          # NEW
        "near_all": near_all,
        "near_month": near_month,
        "near_week": near_week,
        "near_fy": near_fy,      # NEW
        "avg_all": avg_all,
        "avg_month": avg_month,
        "avg_week": avg_week,
        "avg_fy": avg_fy,        # NEW

        # header name
        "employee_name": employee_name,
    })



@require_POST
def update_customer_location(request):
    """
    Queue request; admin will push from /admin.
    Body: {"customer": "<DocName>", "latitude": <float>, "longitude": <float>}
    """
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return JsonResponse({"error": "Not logged in"}, status=401)

    try:
        data = json.loads(request.body or "{}")
        docname = (data.get("customer") or "").strip()
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
    except Exception:
        return JsonResponse({"error": "Invalid payload"}, status=400)

    if not docname:
        return JsonResponse({"error": "Missing customer"}, status=400)

    CustomerLocationUpdate.objects.create(
        customer=docname,
        latitude=lat,
        longitude=lon,
        requested_by=request.user if request.user.is_authenticated else None,
    )

    return JsonResponse(
        {"status": "queued", "message": "Location submitted for admin approval."},
        status=202
    )


# --- Frappe endpoints ---
FRAPPE_LOGIN_URL = getattr(
    settings,
    "FRAPPE_LOGIN_URL",
    "https://eipl.electrolabgroup.com/api/method/login",
)
FRAPPE_BASE_URL = "https://eipl.electrolabgroup.com"



def haversine_km(lat1, lon1, lat2, lon2):

    R = 6378.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@require_GET
def get_my_leads(request):
    """Return leads assigned to the logged-in user."""
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return JsonResponse({"error": "Not logged in"}, status=401)

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    params = {
        "fields": '["name","lead_name","status","company_name","mobile_no","territory"]',
        "filters": json.dumps([
            ["lead_owner", "=", raw_user],
            ["status", "not in", ["Converted", "Do Not Contact"]],
        ]),
        "limit_page_length": 100,
        "order_by": "modified desc",
    }
    r = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Lead", params=params, timeout=10)
    data = (r.json().get("data") if r.ok else []) or []
    return JsonResponse({"leads": data})


@require_GET
def search_leads(request):
    """Search leads by name."""
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return JsonResponse({"error": "Not logged in"}, status=401)

    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"leads": []})

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    fields = '["name","lead_name","status","company_name","mobile_no","territory","lead_owner"]'

    # Search by lead_name OR company_name — run both queries and merge by name
    seen = {}
    for field in ("lead_name", "company_name"):
        params = {
            "fields": fields,
            "filters": json.dumps([[field, "like", f"%{q}%"]]),
            "limit_page_length": 50,
        }
        r = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Lead", params=params, timeout=8)
        for row in (r.json().get("data") if r.ok else []) or []:
            seen.setdefault(row["name"], row)

    return JsonResponse({"leads": list(seen.values())})


@require_GET
def search_customers(request):
    """Search customers by name across the full customer list (not proximity-filtered)."""
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return JsonResponse({"error": "Not logged in"}, status=401)

    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"customers": []})

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    params = {
        "fields": '["name","customer_name","territory","custom_latitude","custom_longitude"]',
        "filters": json.dumps([["customer_name", "like", f"%{q}%"]]),
        "limit_page_length": 50,
    }
    r = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Customer", params=params, timeout=8)
    data = (r.json().get("data") if r.ok else []) or []

    results = []
    for c in data:
        lat, lon = None, None
        try:
            lat = float(c.get("custom_latitude"))
            lon = float(c.get("custom_longitude"))
        except (TypeError, ValueError):
            pass
        results.append({
            "name": c["name"],
            "customer_name": c.get("customer_name") or c["name"],
            "territory": c.get("territory") or "",
            "latitude": lat,
            "longitude": lon,
            "has_location": lat is not None and lon is not None,
        })

    return JsonResponse({"customers": results})


@require_GET
def nearby_customers(request):
    """Return customers near (lat,lon), enriched with open opp count+sum and item names.
       Never fail the nearby list if enrichment has issues."""
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return JsonResponse({"error": "Not logged in"}, status=401)

    # coords
    try:
        user_lat = float(request.GET.get("lat"))
        user_lon = float(request.GET.get("lon"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Missing or invalid lat/lon"}, status=400)

    # optional radius (km), default 5.0
    try:
        radius_km = float(request.GET.get("radius_km", 5.0))
        radius_km = max(1.0, min(radius_km, 100.0))
    except ValueError:
        radius_km = 5.0

    # ERP session
    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    # 1) fetch customers (minimal fields)
    start, page_length = 0, 1000
    customers = []
    while True:
        params = {
            "fields": '["name","customer_name","custom_latitude","custom_longitude"]',
            "limit_start": start,
            "limit_page_length": page_length,
        }
        r = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Customer", params=params, timeout=8)
        data = (r.json().get("data") if r.ok else []) or []
        customers.extend(data)
        if len(data) < page_length:
            break
        start += page_length

    # 2) filter by distance
    nearby = []
    for c in customers:
        try:
            clat = float(c.get("custom_latitude"))
            clon = float(c.get("custom_longitude"))
        except (TypeError, ValueError):
            continue
        d = haversine_km(user_lat, user_lon, clat, clon)
        if d <= radius_km:
            nearby.append({
                "name": c["name"],                                      # docname
                "customer_name": c.get("customer_name") or c["name"],    # display
                "latitude": clat,
                "longitude": clon,
                "distance_km": round(d, 2),
            })

    if not nearby:
        return JsonResponse({"customers": []})

    # 3) enrich with open opportunities (count, sum, currency) + item names
    CLOSED = ["Closed", "Converted", "Lost", "Order Lost", "Order Won"]
    try:
        # Build lookup sets
        docnames = sorted({row["name"] for row in nearby})
        displays = sorted({row["customer_name"] for row in nearby})

        def fetch_open_by(field, values):
            if not values:
                return []
            url = f"{FRAPPE_BASE_URL}/api/resource/Opportunity"
            params = {
                "fields": '["name","status","opportunity_amount","currency","party_name","customer_name"]',
                "filters": json.dumps([
                    [field, "in", values],
                    ["status", "not in", CLOSED],
                ]),
                "limit_page_length": 0,
            }
            resp = sess.get(url, params=params, timeout=12)
            if resp.status_code == 417 or not resp.ok:
                return []
            return resp.json().get("data", []) or []

        by_party   = fetch_open_by("party_name", docnames)
        by_display = fetch_open_by("customer_name", displays)

        # Map display -> docname (first wins)
        display_to_doc = {}
        for row in nearby:
            display_to_doc.setdefault(row["customer_name"], row["name"])

        # ---- DE-DUP opportunities across both result sets, then total once
        def _parse_amount(amt):
            try:
                return float(amt)
            except (TypeError, ValueError):
                try:
                    return float(str(amt).replace(",", ""))
                except Exception:
                    return 0.0

        # opp_name -> (docname, amount_float, currency)
        op_map = {}
        for r in (by_party + by_display):
            opp_name = r.get("name")
            if not opp_name:
                continue
            dn = r.get("party_name") or display_to_doc.get(r.get("customer_name"))
            if not dn:
                continue
            if opp_name in op_map:
                continue
            op_map[opp_name] = (dn, _parse_amount(r.get("opportunity_amount")), r.get("currency"))

        totals, currencies, counts = {}, {}, {}
        for _opp, (dn, val, cur) in op_map.items():
            totals[dn] = totals.get(dn, 0.0) + (val or 0.0)
            counts[dn] = counts.get(dn, 0) + 1
            if dn not in currencies and cur:
                currencies[dn] = cur

        opp_names = list(op_map.keys())

        items_by_opp = {}
        if opp_names:
            def get_items_via_get_list(names_batch):
                url = f"{FRAPPE_BASE_URL}/api/method/frappe.client.get_list"
                params = {
                    "doctype": "Opportunity Item",
                    "fields": '["parent","item_name","item_code"]',
                    "filters": json.dumps([
                        ["parent", "in", names_batch],
                        ["parenttype", "=", "Opportunity"],
                        ["parentfield", "=", "items"]
                    ]),
                    "limit_page_length": 0,
                }
                resp = sess.get(url, params=params, timeout=15)
                if not resp.ok:
                    return []
                return resp.json().get("message", []) or resp.json().get("data", []) or []

            # In case some servers block GET, add a safe fallback to POST
            def get_items_via_post(names_batch):
                url = f"{FRAPPE_BASE_URL}/api/method/frappe.client.get_list"
                payload = {
                    "doctype": "Opportunity Item",
                    "fields": ["parent", "item_name", "item_code"],
                    "filters": [
                        ["parent", "in", names_batch],
                        ["parenttype", "=", "Opportunity"],
                        ["parentfield", "=", "items"]
                    ],
                    "limit_page_length": 0,
                }
                resp = sess.post(url, json=payload, timeout=15)
                if not resp.ok:
                    return []
                return resp.json().get("message", []) or resp.json().get("data", []) or []

            # Batch to avoid very long query strings / payloads
            BATCH = 200
            all_item_rows = []
            for i in range(0, len(opp_names), BATCH):
                batch = opp_names[i:i+BATCH]
                rows = get_items_via_get_list(batch)
                if not rows:
                    rows = get_items_via_post(batch)
                all_item_rows.extend(rows)

            for it in all_item_rows:
                parent = it.get("parent")
                nm = (it.get("item_name") or it.get("item_code") or "").strip()
                if parent and nm:
                    items_by_opp.setdefault(parent, set()).add(nm)

            # As a last-resort fallback (rare): if still nothing, fetch a few full Opp docs
            if not items_by_opp and opp_names:
                sample = opp_names[:25]
                for opp in sample:
                    resp = sess.get(f"{FRAPPE_BASE_URL}/api/resource/Opportunity/{opp}",
                                    params={"fields": '["name","items"]'}, timeout=10)
                    if not resp.ok:
                        continue
                    doc = resp.json().get("data") or {}
                    for ch in (doc.get("items") or []):
                        nm = (ch.get("item_name") or ch.get("item_code") or "").strip()
                        if nm:
                            items_by_opp.setdefault(opp, set()).add(nm)

        # Build items per customer using the unique opp->customer mapping
        open_items_by_customer = {}
        for opp_name, (cust_dn, _val, _cur) in op_map.items():
            for nm in items_by_opp.get(opp_name, set()):
                bucket = open_items_by_customer.setdefault(cust_dn, {})
                key = nm.casefold()
                if key not in bucket:
                    bucket[key] = nm  # keep original casing

        # Attach enrichment to rows
        for row in nearby:
            dn = row["name"]
            row["open_opportunity_count"] = counts.get(dn, 0)
            row["open_opportunity_amount"] = round(totals.get(dn, 0.0), 2)
            row["currency"] = currencies.get(dn)

            items_dict = open_items_by_customer.get(dn, {})
            items_list = sorted(items_dict.values())
            row["open_opportunity_items"] = items_list
            row["open_opportunity_items_text"] = ", ".join(items_list)

    except Exception:
        for row in nearby:
            row.setdefault("open_opportunity_count", 0)
            row.setdefault("open_opportunity_amount", 0.0)
            row.setdefault("open_opportunity_items", [])
            row.setdefault("open_opportunity_items_text", "")

    nearby.sort(
        key=lambda x: (
            -(float(x.get("open_opportunity_amount") or 0.0)),
            float(x.get("distance_km") or 1e9)
        )
    )
    return JsonResponse({"customers": nearby})


@require_POST
def logout_view(request):
    request.session.flush()
    return redirect("frappe_login")


# inside views.py
def frappe_login(request):
    # where to go after login
    next_url = request.GET.get("next") or request.POST.get("next") or reverse("punch")

    # If already have frappe session but not Django-authenticated, auto-login a Django user
    if request.method == "GET" and request.session.get("frappe_user"):
        if not request.user.is_authenticated:
            User = get_user_model()
            username = request.session["frappe_user"]  # this is now an email
            user, _ = User.objects.get_or_create(username=username, defaults={"is_active": True})
            login(request, user)
        return redirect(next_url)

    if request.method == "POST":
        typed_user = request.POST.get("username")
        password = request.POST.get("password")
        remember_me = request.POST.get("remember_me") == "on"

        session = requests.Session()
        resp = session.post(FRAPPE_LOGIN_URL, data={"usr": typed_user, "pwd": password})
        if resp.status_code == 200:
            # --- Resolve canonical ERP email for the logged-in account ---
            erp_email = None

            # 1) Preferred: frappe.auth.get_logged_user -> returns email
            try:
                r0 = session.get(f"{FRAPPE_BASE_URL}/api/method/frappe.auth.get_logged_user", timeout=8)
                if r0.ok:
                    # typical payload: {"message": "user@example.com"}
                    erp_email = (r0.json() or {}).get("message")
            except Exception:
                pass

            # 2) Fallback via User doctype (name is usually the email on ERPNext)
            if not erp_email:
                try:
                    r1 = session.get(
                        f"{FRAPPE_BASE_URL}/api/resource/User",
                        params={
                            "fields": '["name","email","full_name"]',
                            # try both "name" (usually email/login) and "email" fields
                            "filters": json.dumps([
                                ["name", "like", f"{typed_user}%"]
                            ]),
                            "limit_page_length": 1,
                        },
                        timeout=10,
                    )
                    if r1.ok:
                        data = (r1.json() or {}).get("data") or []
                        if data:
                            row = data[0]
                            # Prefer explicit "email"; fall back to "name"
                            erp_email = (row.get("email") or row.get("name") or "").strip() or None
                except Exception:
                    pass

            # 3) Last resort: query Employee.user_id (often the email tied to the ERP user)
            if not erp_email:
                try:
                    r2 = session.get(
                        f"{FRAPPE_BASE_URL}/api/resource/Employee",
                        params={
                            "fields": '["user_id","employee_name"]',
                            "filters": json.dumps([["user_id", "like", f"{typed_user}%"], ["status", "=", "Active"]]),
                            "limit_page_length": 1,
                        },
                        timeout=10,
                    )
                    if r2.ok:
                        data = (r2.json() or {}).get("data") or []
                        if data and (data[0].get("user_id") or "").strip():
                            erp_email = (data[0]["user_id"] or "").strip()
                except Exception:
                    pass

            # Absolute fallback: if everything failed, use the typed value
            erp_email = (erp_email or typed_user).strip().lower()

            # --- Store ERP session and canonical email in our session ---
            request.session["frappe_user"] = erp_email
            request.session["frappe_cookies"] = session.cookies.get_dict()
            request.session.set_expiry(86400 if remember_me else 0)

            # --- Django auth session: ensure user is keyed by ERP email ---
            User = get_user_model()
            user, _ = User.objects.get_or_create(username=erp_email, defaults={"is_active": True})
            login(request, user)

            return redirect(next_url)

    return render(request, "login.html", {"next": request.GET.get("next", "")})





@require_http_methods(["GET", "POST"])
def punch(request):
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return redirect("frappe_login")

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    def get_employee():
        filters = [["user_id", "like", f"{raw_user}%"], ["status", "=", "Active"]]
        url = f"{FRAPPE_BASE_URL}/api/resource/Employee"
        params = {
            "fields": '["name","employee_name"]',
            "filters": json.dumps(filters),
            "limit_page_length": 1,
        }
        resp = sess.get(url, params=params, timeout=10)
        if not resp.ok:
            return None, resp.text
        data = resp.json().get("data", [])
        if not data:
            return None, "No active Employee found"
        return data[0], None

    if request.method == "GET":
        # Lead mode: skip the open-checkin redirect, render with lead context
        if request.GET.get("mode") == "lead":
            emp, err = get_employee()
            context = {
                "employee_name": (emp or {}).get("employee_name", raw_user) if not err else raw_user,
                "lead_mode": True,
                "active_lead": request.GET.get("lead", ""),
            }
            return render(request, "punch.html", context)

        emp, err = get_employee()
        if not err and emp:
            try:
                last_resp = sess.get(
                    f"{FRAPPE_BASE_URL}/api/resource/Employee Checkin",
                    params={
                        "fields": '["name","log_type","time"]',
                        "filters": json.dumps([["employee", "=", emp["name"]]]),
                        "order_by": "time desc",
                        "limit_page_length": 1,
                    },
                    timeout=10,
                )
                last = (last_resp.json().get("data") or [])
                if last and last[0].get("log_type") == "IN":
                    last_customer = ""
                    last_lead = ""
                    try:
                        doc_resp = sess.get(
                            f"{FRAPPE_BASE_URL}/api/resource/Employee Checkin/{quote(last[0]['name'])}",
                            params={"fields": '["customer","remark"]'},
                            timeout=8,
                        )
                        doc = (doc_resp.json().get("data") or {})
                        last_customer = (doc.get("customer") or "").strip()
                        remark = (doc.get("remark") or "")
                        for part in remark.split("|"):
                            part = part.strip()
                            if not last_customer and part.lower().startswith("customer:"):
                                last_customer = part[len("customer:"):].strip()
                            if part.lower().startswith("lead:"):
                                last_lead = part[len("lead:"):].strip()
                    except Exception:
                        pass

                    if last_lead:
                        return redirect(f"{reverse('punch')}?mode=lead&lead={quote(last_lead)}")

                    dest = reverse("select_customer")
                    if last_customer:
                        dest = f"{dest}?customer={quote(last_customer)}"
                    return redirect(dest)
            except Exception:
                pass

        context = {
            "employee_name": (emp or {}).get("employee_name", raw_user)
            if not err
            else raw_user
        }
        return render(request, "punch.html", context)

    # ---------- POST ----------
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "detail": "Invalid JSON"}, status=400)

    emp, err = get_employee()
    if err or not emp:
        return JsonResponse({"status": "error", "detail": err}, status=400)

    time_str = data.get("time", "")
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        time_str = time_str.split(".")[0].replace("T", " ")

    cust = (data.get("customer") or "").strip()
    opp = (data.get("opportunity") or "").strip()
    lead = (data.get("lead") or "").strip()
    free_remark = (data.get("remark_free") or "").strip()
    assign_to_email = (data.get("assign_to_email") or "").strip()

    # Remark logic
    if lead and not cust:
        remark_text = f"Lead: {lead}"
    elif free_remark and not cust:
        remark_text = free_remark
    else:
        remark_text = f"Customer: {cust}" if cust else ""
        if opp:
            remark_text = f"{remark_text} | Opportunity: {opp}" if remark_text else f"Opportunity: {opp}"

    checkin_payload = {
        "doctype": "Employee Checkin",
        "employee": emp["name"],
        "log_type": data.get("log_type"),
        "time": time_str,
        "longitude": data.get("longitude"),
        "latitude": data.get("latitude"),
        "city": data.get("city"),
        "state": data.get("state"),
        "area": data.get("area"),
        "customer": data.get("customer"),
        "opportunity": data.get("opportunity"),
        "remark": remark_text,
    }

    checkin_resp = sess.post(
        f"{FRAPPE_BASE_URL}/api/resource/{quote(checkin_payload['doctype'])}",
        json=checkin_payload,
        timeout=15,
    )
    if not checkin_resp.ok:
        return JsonResponse(
            {"status": "error", "detail": checkin_resp.text},
            status=checkin_resp.status_code,
        )

    # ----- PUNCH IN -----
    if data.get("log_type") == "IN":
        if lead:
            next_url = f"{reverse('punch')}?mode=lead&lead={quote(lead)}"
        else:
            next_url = reverse("select_customer")
            if cust:
                next_url = f"{next_url}?customer={quote(cust)}"

        award_points(
            request.user,
            2,
            EnergyPointTransaction.Reason.PUNCH_IN,
            {"checkin_id": checkin_resp.json().get("data", {}).get("name")}
        )
        return JsonResponse({"status": "success", "next_url": next_url})

    # ----- PUNCH OUT -----
    if data.get("log_type") == "OUT":
        # Lead punch-out: simplified flow (no opportunity/meeting ERP push)
        if lead:
            try:
                sess2 = requests.Session()
                sess2.cookies.update(request.session.get("frappe_cookies", {}))
                token2 = getattr(settings, "FRAPPE_API_TOKEN", None)
                if token2:
                    sess2.headers.update({"Authorization": f"token {token2}"})
                emp_name = emp["name"]
                last_params = {
                    "fields": '["name","log_type","latitude","longitude"]',
                    "filters": json.dumps([["employee", "=", emp_name], ["log_type", "=", "IN"]]),
                    "order_by": "time desc",
                    "limit_page_length": 1,
                }
                last_resp = sess2.get(f"{FRAPPE_BASE_URL}/api/resource/Employee Checkin",
                                      params=last_params, timeout=10)
                last_data = (last_resp.json().get("data") or [])
                near = False
                d_km = None
                if last_data and last_data[0].get("latitude") and last_data[0].get("longitude"):
                    lat_in = float(last_data[0]["latitude"])
                    lon_in = float(last_data[0]["longitude"])
                    lat_out = float(data.get("latitude") or 0)
                    lon_out = float(data.get("longitude") or 0)
                    d_km = haversine_km(lat_in, lon_in, lat_out, lon_out)
                    near = (d_km <= 0.2)
                award_points(
                    request.user,
                    2 if near else 1,
                    EnergyPointTransaction.Reason.PUNCH_OUT_NEAR if near else EnergyPointTransaction.Reason.PUNCH_OUT_FAR,
                    {"distance_km": round(d_km, 4) if d_km is not None else None, "lead": lead}
                )
            except Exception:
                award_points(request.user, 1, EnergyPointTransaction.Reason.PUNCH_OUT_FAR, {"lead": lead})
            return JsonResponse({"status": "success", "next_url": reverse("punch")})

        meetings = data.get("meeting_details", []) or []
        multi = data.get("meeting_details_by_opportunity") or {}

        # Save meeting details to Opportunity in ERP
        if multi:
            for opp_name, entries in multi.items():
                if not opp_name or not entries:
                    continue
                child_rows = [
                    {
                        "doctype": "Opportunity Meeting Detail",
                        "agenda": (m.get("agenda") or ""),
                        "visited_department": (m.get("visited_department") or ""),
                        "contact_person": (m.get("contact_person") or ""),
                        "notes": (m.get("notes") or ""),
                        "idx": idx,
                    }
                    for idx, m in enumerate(entries, start=1)
                ]
                update_url = f"{FRAPPE_BASE_URL}/api/resource/Opportunity/{quote(opp_name)}"
                update_resp = sess.put(update_url, json={"meeting_details": child_rows}, timeout=15)
                if not update_resp.ok:
                    return JsonResponse({"status": "error", "detail": update_resp.text},
                                        status=update_resp.status_code)
        elif meetings and data.get("opportunity"):
            child_rows = [
                {
                    "doctype": "Opportunity Meeting Detail",
                    "agenda": m.get("agenda", ""),
                    "visited_department": m.get("visited_department", ""),
                    "contact_person": m.get("contact_person", ""),
                    "notes": m.get("notes", ""),
                    "idx": idx,
                }
                for idx, m in enumerate(meetings, start=1)
            ]
            update_url = f"{FRAPPE_BASE_URL}/api/resource/Opportunity/{quote(data['opportunity'])}"
            update_resp = sess.put(update_url, json={"meeting_details": child_rows}, timeout=15)
            if not update_resp.ok:
                return JsonResponse({"status": "error", "detail": update_resp.text},
                                    status=update_resp.status_code)

        # Local persistence of meeting details
        erp_data = checkin_resp.json().get("data", {}) or {}
        checkin_id = erp_data.get("name")

        if multi:
            for opp_name, entries in multi.items():
                for m in (entries or []):
                    try:
                        OpportunityMeetingDetail.objects.create(
                            user=request.user if request.user.is_authenticated else None,
                            checkin_id=checkin_id or "",
                            customer=data.get("customer") or "",
                            opportunity=opp_name or "",
                            agenda=m.get("agenda", ""),
                            visited_department=m.get("visited_department", ""),
                            contact_person=m.get("contact_person", ""),
                            notes=m.get("notes", ""),
                        )
                    except Exception:
                        pass
        else:
            for m in meetings:
                try:
                    OpportunityMeetingDetail.objects.create(
                        user=request.user if request.user.is_authenticated else None,
                        checkin_id=checkin_id or "",
                        customer=data.get("customer") or "",
                        opportunity=data.get("opportunity") or "",
                        agenda=m.get("agenda", ""),
                        visited_department=m.get("visited_department", ""),
                        contact_person=m.get("contact_person", ""),
                        notes=m.get("notes", ""),
                    )
                except Exception:
                    pass

        # ---------- ASSIGN-TO (robust: try assign_to.add, then fallback to ToDo) ----------
        if assign_to_email and checkin_id:
            try:
                # Try Frappe's assign_to API
                assign_url = f"{FRAPPE_BASE_URL}/api/method/frappe.desk.form.assign_to.add"
                assign_payload = {
                    "doctype": "Employee Checkin",
                    "name": checkin_id,
                    "assign_to": [assign_to_email],   # list of emails
                    "description": "Assigned from Punch Out",
                    "notify": 1,
                    "priority": "Medium",
                }
                assign_resp = sess.post(assign_url, json=assign_payload, timeout=10)

                # Some stacks require 'args' (as JSON string) instead of raw JSON:
                if not assign_resp.ok:
                    assign_resp2 = sess.post(
                        assign_url,
                        data={"args": json.dumps(assign_payload)},
                        timeout=10
                    )
                    if not assign_resp2.ok:
                        # Fallback: create a ToDo instead (non-blocking)
                        todo_payload = {
                            "doctype": "ToDo",
                            "allocated_to": assign_to_email,
                            "status": "Open",
                            "priority": "Medium",
                            "description": f"Follow-up for check-out: {(emp.get('employee_name') or emp.get('name') or '').strip()}",
                            "reference_type": "Employee Checkin",
                            "reference_name": checkin_id,
                        }
                        sess.post(f"{FRAPPE_BASE_URL}/api/resource/ToDo", json=todo_payload, timeout=10)
            except Exception:
                # Last-resort fallback—do not break punch-out
                try:
                    todo_payload = {
                        "doctype": "ToDo",
                        "allocated_to": assign_to_email,
                        "status": "Open",
                        "priority": "Medium",
                        "description": f"Follow-up for check-out: {(emp.get('employee_name') or emp.get('name') or '').strip()}",
                        "reference_type": "Employee Checkin",
                        "reference_name": checkin_id,
                    }
                    sess.post(f"{FRAPPE_BASE_URL}/api/resource/ToDo", json=todo_payload, timeout=10)
                except Exception:
                    pass

        # ---------- NEAR (200 m) check + remark update on last IN ----------
        try:
            sess2 = requests.Session()
            sess2.cookies.update(request.session.get("frappe_cookies", {}))
            token = getattr(settings, "FRAPPE_API_TOKEN", None)
            if token:
                sess2.headers.update({"Authorization": f"token {token}"})

            emp_name = emp["name"]

            last_params = {
                "fields": '["name","log_type","latitude","longitude","time"]',
                "filters": json.dumps([["employee", "=", emp_name], ["log_type", "=", "IN"]]),
                "order_by": "time desc",
                "limit_page_length": 1,
            }
            last_resp = sess2.get(f"{FRAPPE_BASE_URL}/api/resource/Employee Checkin", params=last_params, timeout=10)
            last_data = (last_resp.json().get("data") or [])

            near = False
            d_km = None
            if last_data and last_data[0].get("latitude") and last_data[0].get("longitude"):
                lat_in = float(last_data[0]["latitude"])
                lon_in = float(last_data[0]["longitude"])
                lat_out = float(data.get("latitude") or 0)
                lon_out = float(data.get("longitude") or 0)
                d_km = haversine_km(lat_in, lon_in, lat_out, lon_out)
                near = (d_km <= 0.2)

                if near and last_data[0].get("name"):
                    in_docname = last_data[0]["name"]
                    put_url = f"{FRAPPE_BASE_URL}/api/resource/Employee Checkin/{quote(in_docname)}"
                    sess2.put(put_url, json={"remark": remark_text}, timeout=10)

            award_points(
                request.user,
                2 if near else 1,
                EnergyPointTransaction.Reason.PUNCH_OUT_NEAR if near else EnergyPointTransaction.Reason.PUNCH_OUT_FAR,
                {"distance_km": round(d_km, 4) if d_km is not None else None}
            )
        except Exception:
            award_points(request.user, 1, EnergyPointTransaction.Reason.PUNCH_OUT_FAR, {"distance_km": None})

        # Extra points if meetings added
        has_meetings = bool(meetings) or any((len(v or []) for v in (multi or {}).values()))
        if has_meetings:
            award_points(
                request.user, 3, EnergyPointTransaction.Reason.MEETING_DETAILS,
                {
                    "single_count": len(meetings or []),
                    "multi_counts": {k: len(v or []) for k, v in (multi or {}).items()}
                }
            )

        return JsonResponse({"status": "success", "next_url": reverse("punch")})




@require_http_methods(["GET"])
def select_customer(request):
    raw_user = request.session.get("frappe_user")
    if not raw_user:
        return redirect("frappe_login")

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    pre_cust = (request.GET.get("customer") or request.GET.get("customer_name") or "").strip()


    filters = [["user_id", "like", f"{raw_user}%"], ["status", "=", "Active"]]
    resp = sess.get(
        f"{FRAPPE_BASE_URL}/api/resource/Employee",
        params={
            "fields": '["employee_name","name"]',
            "filters": json.dumps(filters),
            "limit_page_length": 1,
        },
        timeout=5,
    )
    if resp.ok and (data := resp.json().get("data")):
        employee_name = data[0].get("employee_name") or raw_user
        emp_name = data[0].get("name")
    else:
        employee_name = raw_user
        emp_name = None

    # Get last check-in location to optionally filter by proximity
    user_lat, user_lon = None, None
    if emp_name:
        last_url = f"{FRAPPE_BASE_URL}/api/resource/Employee Checkin"
        last_params = {
            "fields": '["latitude","longitude"]',
            "filters": json.dumps([["employee", "=", emp_name]]),
            "order_by": "time desc",
            "limit_page_length": 1,
        }
        last_resp = sess.get(last_url, params=last_params, timeout=10)
        last_data = last_resp.json().get("data", [])
        if last_data:
            user_lat = last_data[0].get("latitude")
            user_lon = last_data[0].get("longitude")

    # Fetch all customers
    all_customers = []
    start, page_length = 0, 1000
    while True:
        params = {
            "fields": '["name","customer_name","custom_latitude","custom_longitude"]',
            "limit_start": start,
            "limit_page_length": page_length,
        }
        batch = (
            sess.get(f"{FRAPPE_BASE_URL}/api/resource/Customer", params=params, timeout=5)
            .json()
            .get("data", [])
        )
        all_customers.extend(batch)
        if len(batch) < page_length:
            break
        start += page_length

    if pre_cust:
        present = next(
            (c for c in all_customers if c.get("name") == pre_cust or c.get("customer_name") == pre_cust),
            None,
        )
        if not present:
            # Try exact doc fetch first
            extra = None
            r = sess.get(
                f"{FRAPPE_BASE_URL}/api/resource/Customer/{quote(pre_cust)}",
                params={"fields": '["name","customer_name"]'},
                timeout=6,
            )
            if r.ok and r.json().get("data"):
                d = r.json()["data"]
                extra = {"name": d.get("name"), "customer_name": d.get("customer_name")}
            else:
                # Fallback: search by display name
                sr = sess.get(
                    f"{FRAPPE_BASE_URL}/api/resource/Customer",
                    params={
                        "fields": '["name","customer_name"]',
                        "filters": json.dumps([["customer_name", "=", pre_cust]]),
                        "limit_page_length": 1,
                    },
                    timeout=6,
                )
                rows = (sr.json().get("data") if sr.ok else []) or []
                if rows:
                    extra = {"name": rows[0]["name"], "customer_name": rows[0]["customer_name"]}
            if extra:
                all_customers.append(extra)

    return render(
        request,
        "select_customer.html",
        {"employee_name": employee_name, "customers": all_customers},
    )


# --- Opportunity helpers ---
@require_GET
def get_opportunities(request):
    customer = request.GET.get("customer")
    if not customer:
        return JsonResponse({"error": "Missing customer parameter"}, status=400)

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    url = f"{FRAPPE_BASE_URL}/api/resource/Opportunity"
    filters = [
        ["party_name", "=", customer],
        ["status", "not in", ["Closed", "Converted", "Lost", "Order Lost", "Order Won"]],
    ]
    params = {
        "fields": '["name","status"]',
        "filters": json.dumps(filters),
        "limit_page_length": 0,
    }

    resp = sess.get(url, params=params, timeout=5)
    if resp.status_code == 417:
        return JsonResponse({"opportunities": []})
    if not resp.ok:
        return JsonResponse({"error": resp.text}, status=resp.status_code)

    return JsonResponse({"opportunities": resp.json().get("data", [])})


@require_GET
def get_opportunity_items(request):
    opportunity = request.GET.get("opportunity")
    if not opportunity:
        return JsonResponse({"error": "Missing opportunity parameter"}, status=400)

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    url = f"{FRAPPE_BASE_URL}/api/resource/Opportunity/{quote(opportunity)}"
    params = {"fields": '["name","items"]'}
    resp = sess.get(url, params=params, timeout=5)
    if not resp.ok:
        return JsonResponse({"error": resp.text}, status=resp.status_code)

    data = resp.json().get("data", {})
    items = data.get("items", [])
    result = [{"item_name": it.get("item_name")} for it in items]
    return JsonResponse({"items": result})




@staff_member_required
@require_POST
def admin_push_customer_location(request):
    """
    ADMIN-ONLY: push a queued location directly to ERP.
    Body: {"customer": "<DocName>", "latitude": <float>, "longitude": <float>}
    """
    try:
        data = json.loads(request.body or "{}")
        docname = (data.get("customer") or "").strip()
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
    except Exception:
        return JsonResponse({"error": "Invalid payload"}, status=400)
    if not docname:
        return JsonResponse({"error": "Missing customer"}, status=400)

    sess = requests.Session()
    sess.cookies.update(request.session.get("frappe_cookies", {}))
    if token := getattr(settings, "FRAPPE_API_TOKEN", None):
        sess.headers.update({"Authorization": f"token {token}"})

    url = f"{FRAPPE_BASE_URL}/api/resource/Customer/{quote(docname)}"
    payload = {"custom_latitude": lat, "custom_longitude": lon}
    resp = sess.put(url, json=payload, timeout=12)
    if not resp.ok:
        return JsonResponse({"error": resp.text}, status=resp.status_code)
    return JsonResponse({"status": "ok"})



