const toast = document.querySelector(".toast");

function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function formatMoney(value) {
    const amount = Number(value || 0);
    return `Rs. ${amount.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

async function postJson(url, payload) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
        throw new Error(data.message || "Something went wrong.");
    }
    return data;
}

document.querySelector(".nav-toggle")?.addEventListener("click", () => {
    document.querySelector(".nav-links")?.classList.toggle("open");
});

document.querySelectorAll(".add-to-cart").forEach((button) => {
    button.addEventListener("click", async () => {
        const productCard = button.closest(".product-card, .product-detail");
        const quantityInput = productCard?.querySelector("input[type='number']");
        const quantity = Number(quantityInput?.value || 1);
        button.disabled = true;
        try {
            const data = await postJson("/api/cart/add", {
                product_id: Number(button.dataset.productId),
                quantity
            });
            document.querySelectorAll(".cart-count").forEach((badge) => {
                badge.textContent = data.cart_count;
            });
            showToast(data.message);
        } catch (error) {
            showToast(error.message);
        } finally {
            button.disabled = false;
        }
    });
});

async function updateCartRow(row, quantity) {
    const cartId = Number(row.dataset.cartId);
    const data = await postJson("/api/cart/update", { cart_id: cartId, quantity });
    row.querySelector("input[type='number']").value = quantity;
    const unitPrice = Number(row.dataset.unitPrice || 0);
    row.querySelector(".line-total").textContent = formatMoney(unitPrice * quantity);
    document.querySelector("#cart-subtotal").textContent = formatMoney(data.subtotal);
    document.querySelector("#cart-delivery").textContent = formatMoney(data.delivery_charge);
    document.querySelector("#cart-total").textContent = formatMoney(data.total);
    document.querySelectorAll(".cart-count").forEach((badge) => {
        badge.textContent = data.cart_count;
    });
}

document.querySelectorAll(".cart-item").forEach((row) => {
    const input = row.querySelector("input[type='number']");

    row.querySelectorAll(".qty-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            const current = Number(input.value || 1);
            const change = Number(button.dataset.change || 0);
            const next = Math.max(1, Math.min(Number(input.max || 99), current + change));
            try {
                await updateCartRow(row, next);
            } catch (error) {
                showToast(error.message);
            }
        });
    });

    input?.addEventListener("change", async () => {
        const next = Math.max(1, Math.min(Number(input.max || 99), Number(input.value || 1)));
        try {
            await updateCartRow(row, next);
        } catch (error) {
            showToast(error.message);
        }
    });

    row.querySelector(".remove-cart-item")?.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/cart/remove", { cart_id: Number(row.dataset.cartId) });
            row.remove();
            document.querySelectorAll(".cart-count").forEach((badge) => {
                badge.textContent = data.cart_count;
            });
            if (!document.querySelector(".cart-item")) {
                window.location.reload();
            } else {
                const remainingRows = [...document.querySelectorAll(".cart-item")];
                const subtotal = remainingRows.reduce((sum, item) => {
                    return sum + Number(item.dataset.unitPrice || 0) * Number(item.querySelector("input").value || 0);
                }, 0);
                const delivery = subtotal >= 499 || subtotal === 0 ? 0 : 39;
                document.querySelector("#cart-subtotal").textContent = formatMoney(subtotal);
                document.querySelector("#cart-delivery").textContent = formatMoney(delivery);
                document.querySelector("#cart-total").textContent = formatMoney(subtotal + delivery);
            }
            showToast("Item removed from cart.");
        } catch (error) {
            showToast(error.message);
        }
    });
});
