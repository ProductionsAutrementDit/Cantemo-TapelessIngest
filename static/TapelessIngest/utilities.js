! function($) {
    $.fn.AjaxCollectionSelect = function(_options) {
        var defaults = {
                minimumInputLength: 2,
                separator: "*valsep*",
                keysep: "*keysep*",
                multiple: !0,
                unique: !0,
                allowNew: !0,
                selectOnBlur: !0,
                quietMillis: 100
            },
            options = $.extend(defaults, _options);
        return this.each(function() {
            var obj = $(this);
            obj.select2({
                minimumInputLength: options.minimumInputLength,
                separator: options.separator,
                multiple: options.multiple,
                unique: options.unique,
                allowNew: options.allowNew,
                quietMillis: options.quietMillis,
                selectOnBlur: options.selectOnBlur,
                placeholder: function() {
                    $(this).data("placeholder")
                },
                initSelection: function(element, callback) {
                    var data = [];
                    $(element.val().split(options.separator)).each(function() {
                        var a = this.split(options.keysep);
                        data.push({
                            id: a[0],
                            text: a[1]
                        })
                    }), callback(data)
                },
                formatResultCssClass: function(object) {
                    return options.allowNew && 0 == object.id.indexOf("*-new-*") ? "select2-result-new" : void 0
                },
                formatSelectionCssClass: function(object) {
                    return options.allowNew && 0 == object.id.indexOf("*-new-*") ? "select2-search-choice-new" : void 0
                },
                createSearchChoice: function(term, data) {
                    return obj.attr("search") ? !1 : options.allowNew && 0 === $(data).filter(function() {
                        return 0 === this.text.localeCompare(term)
                    }).length ? {
                        id: "*-new-*" + term,
                        text: term,
                        isNew: !0
                    } : void 0
                },
                formatResult: function(term) {
                    return term.isNew ? "New collection: " + term.text : term.text
                },
                ajax: {
                    url: obj.attr("rel"),
                    type: "GET",
                    data: function(term, page) {
                        return {
                            query: term
                        }
                    },
                    results: function(data, page) {
                        var itemsArr = data.items.map(function(collection) {
                            for (var key in collection) return {
                                id: key,
                                text: collection[key]
                            }
                        });
                        return {
                            results: itemsArr
                        }
                    }
                }
            })
        })
    }
}(jQuery),
function($) {
    $.fn.AjaxTagSelect = function(_options) {
        var defaults = {
                minimumInputLength: 2,
                separator: "*valsep*",
                keysep: "*keysep*"
            },
            options = $.extend(defaults, _options);
        return this.each(function() {
            var obj = $(this);
            
            valsep = options.separator,
            keysep = options.keysep,
            obj.select2({
                minimumInputLength: function() {
                    var d = $(self).data("minimumInputLength");
                    return void 0 === d ? 2 : d
                },
                separator: valsep,
                multiple: !0,
                unique: !0,
                quietMillis: 100,
                selectOnBlur: !0,
                initSelection: function(element, callback) {
                    var data = [];
                    $(element.val().split(valsep)).each(function() {
                        var key, val;
                        if (-1 != this.indexOf(keysep)) {
                            var a = this.split(keysep);
                            key = a[0], val = a[1]
                        } else key = this, val = this;
                        data.push({
                            id: key,
                            text: val
                        })
                    }), callback(data)
                },
                ajax: {
                    url: obj.attr("rel"),
                    data: function(term, page) {
                        return {
                            stringval: ".*" + term + ".*",
                            format: "json"
                        }
                    },
                    results: function(data, page) {
                        for (var ret = [], i = 0; i < data.length; i++) ret.push({
                            id: data[i].key,
                            text: data[i].value
                        });
                        return {
                            results: ret
                        }
                    }
                }
            })
        })
    }
}(jQuery),
function(Collection, $, undefined) {
    Collection.TapelessIngestaddToCollection = Backbone.View.extend({
        tagName: "div",
        events: {},
        initialize: function(options) {
            this.collectionaddbuttontext = options.collectionaddbuttontext || gettext("Add To Collection"), 
            this.collectioncancelbuttontext = options.collectioncancelbuttontext || gettext("Cancel"), 
            this.title = options.title || gettext("Add To Collection"), 
            this.formURL = options.formURL || "/vs/collections/addtocollectionform/", 
            this.searchCollectionsURL = options.searchCollectionsURL || "/API/v2/collections/autocomplete/", 
            this.dialogButtons = {}, 
            this.dialogOptions = options.dialogoptions || {}, 
            this.getForm(), this.open(), 
            this.selected_objects = options.selected_objects || [], 
            this.library_selected = options.library_selected || undefined, 
            this.search_id_selected = options.search_id_selected || undefined, 
            this.ignore_list = options.ignore_list || [], 
            this.from_collection = options.from_collection || !1
        },
        getForm: function() {
            var self = this;
            $.get(this.formURL, function(data) {
                self.$el.html(data), 
                self.$smartSelectBox = self.$el.find("#collectionselect"), 
                self.smartSelectBox = $(self.$smartSelectBox).AjaxCollectionSelect({
                    valsep: "*valsep*",
                    keysep: "*keysep*"
                })
                 
                self.$smartSelectBox2 = self.$el.find("#tagselect"), 
                self.smartSelectBox2 = $(self.$smartSelectBox2).cantemoSelect2({
                  createSearchChoice: function(term, data) {
                    if (self.$el.attr("search")) {
                      return false;
                    }
                    if ($(data).filter(function() {
                      return this.text.localeCompare(term)===0;
                    }).length===0) {
                      return {id:term, text:term};
                    }
                  }
                })
            })
        },
        open: function() {
            var standardOptions, self = this;
            self.dialogButtons = [{
                text: self.collectionaddbuttontext,
                click: function() {
                    self.add()
                },
                "class": "add-to-collection-button ui-dialog-button-confirm"
            }, {
                text: self.collectioncancelbuttontext,
                click: function() {
                    self.close()
                },
                "class": "cancel-add-to-collection-button ui-dialog-button-cancel"
            }], standardOptions = {
                modal: !0,
                resizable: !1,
                dialogClass: "addToCollection",
                title: self.title,
                minWidth: "450",
                minHeight: "200",
                show: {
                    effect: "fade",
                    duration: 500
                },
                hide: {
                    effect: "fade",
                    duration: 500
                },
                buttons: self.dialogButtons
            };
            for (var attrname in self.dialogOptions) standardOptions[attrname] = self.dialogOptions[attrname];
            self.$el.dialog(standardOptions)
        },
        add: function() {
            var self = this,
                form = this.$el.find("form#collection_add_form");
            self.smartSelectBox ? (formdata = {
                selected_objects: self.selected_objects,
                library_selected: self.library_selected,
                search_id_selected: self.search_id_selected,
                ignore_list: self.ignore_list,
                collection: self.smartSelectBox.val(),
                tags: form.find("#tagselect").val(),
                collectionprofilegroup: form.find("#collectionprofilechooser").val(),
                from_collection: self.from_collection
            }, $.ajax({
                type: "POST",
                url: form.attr("action"),
                data: formdata,
                traditional: !0,
                success: function(responseText, statusText, xhr, $form) {
                    $.growl(responseText.success, "success"), self.close()
                },
                error: function(responseText, statusText, xhr, $form) {
                    $.growl(JSON.parse(responseText.responseText).error, "error")
                }
            })) : $.growl("There was an error sending to the backend", "error")
        },
        close: function() {
            this.$el.dialog("close"), this.undelegateEvents(), $(this.el).removeData().unbind(), this.remove(), Backbone.View.prototype.remove.call(this)
        }
    })
}(cntmo.prtl.Collection = cntmo.prtl.Collection || {}, jQuery);