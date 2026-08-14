# src/create_example_artifact.py

import sys
sys.path.insert(0, '..')

from artifact_schema import Artifact, Step, Locator, Checkpoint
from datetime import datetime

# Create artifact from your manual test
artifact = Artifact(
    version="1.0",
    name="login_and_add_expensive_item",
    description="Login to Saucedemo, find most expensive item (Fleece Jacket $49.99), add to cart",
    target_app="https://www.saucedemo.com",
    inputs={
        "username": "standard_user",
        "password": "secret_sauce"
    },
    steps=[
        Step(1, "navigate", None, None, "Navigate to Saucedemo"),
        Step(2, "click", Locator("id", "user-name", "ID stable"), None, "Click username field"),
        Step(3, "type", Locator("id", "user-name", "ID stable"), "standard_user", "Type username"),
        Step(4, "click", Locator("id", "password", "ID stable"), None, "Click password field"),
        Step(5, "type", Locator("id", "password", "ID stable"), "secret_sauce", "Type password"),
        Step(6, "click", Locator("id", "login-button", "ID stable"), None, "Click login button"),
        Step(7, "click", Locator("text", "Sauce Labs Fleece Jacket", "Product name stable"), None, "Click Fleece Jacket"),
        Step(8, "click", Locator("id", "add-to-cart", "Button ID stable"), None, "Click Add to cart"),
        Step(9, "click", Locator("class", "shopping_cart_link", "Cart icon stable"), None, "Click cart icon"),
    ],
    checkpoint=Checkpoint(
        Locator("class", "cart_quantity", "Cart qty stable"),
        "1",
        "Verify 1 item in cart"
    ),
    outputs={
        "item_added": "true",
        "item_name": "Sauce Labs Fleece Jacket",
        "item_price": "$49.99",
        "cart_item_count": "1"
    },
    created_at=datetime.now().isoformat(),
    created_by="ai_discovery_run",
    estimated_duration_seconds=25
)

# Save it
artifact.save_to_file("../artifacts/saucedemo_login_and_add_to_cart_v1.json")
print("✅ Artifact created and saved!")