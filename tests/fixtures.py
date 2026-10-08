"""Trimmed get_issue_details response, based on a real Issue against the tracey-shop demo app.
Ids and cluster names are made up."""
import copy

ISSUE_ID = "11111111-2222-4333-8444-555555555555"

ISSUE_DETAILS = {
    "id": ISSUE_ID, "name": "Malfunction", "state": "ACTIVE",
    "severity": "Medium", "peak_severity": "Critical", "started_at": "2026-09-23T02:14:54Z",
    "diagnosis_count": 103,
    "entity": {"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "name": "tracey-shop-payment-gw", "type": "Service"},
    "primary_diagnosis": {
        "id": "99999999-8888-4777-8666-555555555555", "name": "Malfunction", "severity": "Medium",
        "entity": {"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "name": "tracey-shop-payment-gw", "type": "Service"},
    },
    "primary_diagnosis_detail": {
        "name": "Service Malfunction",
        "entity": {"labels": {"causely.ai/cluster": "demo-cluster", "k8s.cluster.name": "demo-cluster"}},
        "entity_causal_chain": {"nodes": [
            {"entity": {"name": "tracey-shop-payment-gw"}, "hops": 0,
             "signals": [{"name": "Malfunction"}, {"name": "RequestErrorRate_High"}]},
            {"entity": {"name": "shop.v1.CheckoutService/PlaceOrder"}, "hops": 1,
             "signals": [{"name": "RequestErrorRate_High"}]},
        ]},
        "impacted_services": [{"name": "tracey-shop/checkout"}, {"name": "tracey-shop-payment-gw"}],
        "symptoms": [
            {"active": True, "id": "RequestErrorRate_High::aaaa::1", "name": "Request Error Rate",
             "entity": {"name": "tracey-shop-payment-gw"}},
            {"active": True, "id": "RequestErrorRate_High::bbbb::1", "name": "RPC Request Error Rate",
             "entity": {"name": "shop.v1.CheckoutService/PlaceOrder"}},
        ],
    },
}


def details(**overrides):
    d = copy.deepcopy(ISSUE_DETAILS)
    d.update(overrides)
    return d
