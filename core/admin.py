from django.contrib import admin
from core.models import Account, Asset, Holding, PricePoint, Transaction

admin.site.register([Asset, Account, Transaction, Holding, PricePoint])
